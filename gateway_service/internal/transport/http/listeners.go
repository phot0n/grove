package http

import (
	"crypto/tls"
	"net/http"
	"time"

	"grove-gateway/internal/config"
)

// The two listeners that replace both shapes of the nginx config this gateway used to render.
//
// Host routing needs no code beyond what ServeMux already does: a Go 1.22 pattern takes a host, so
// the two server blocks become two patterns on one mux. Customer traffic answers on the shared name
// that GeoDNS points at whichever gateway is nearest; the admin plane and the scrape answer on this
// box's own name, which reaches this box and no other.

// Handlers builds the mux for the TLS listener.
func (s *Server) Handlers(cfg config.Config, chain []string) (http.Handler, error) {
	data, err := s.DataHandler(chain)
	if err != nil {
		return nil, err
	}
	admin := s.AdminHandler()
	mux := http.NewServeMux()

	// Answered on every name: a health checker asks by this box's name, and anything in front asks
	// by the shared one.
	mux.HandleFunc("/healthz", s.Health)

	if cfg.SelfHost != "" {
		mux.Handle(cfg.SelfHost+"/grove-admin/", admin)
		mux.HandleFunc(cfg.SelfHost+"/healthz", s.Health)
	}
	if cfg.PublicHost != "" && cfg.PublicHost != cfg.SelfHost {
		mux.Handle(cfg.PublicHost+"/", data)
		mux.HandleFunc(cfg.PublicHost+"/healthz", s.Health)
	}
	// The bare patterns are the default for anything that arrived without a matching name — a
	// scrape that connects to the IP and sends no SNI, or a fleet with no zone where both planes
	// share one name.
	mux.Handle("/grove-admin/", admin)
	mux.Handle("/", data)

	if metrics, err := newMetricsProxy(cfg.HtpasswdPath, cfg.NodeExporterURL); err == nil {
		if !cfg.ServesTLS() {
			// Said once, loudly, because it is the cost of not carrying a self-signed certificate:
			// the scrape presents its password on every request, and with no TLS that is in the
			// clear every 15 seconds. The fleet wildcard is what fixes it — this box has no zone.
			s.log.Warn("serving /metrics/node without TLS — the scrape credential is on the wire in "+
				"the clear on every poll; set a Fleet Zone so this box gets the wildcard",
				"htpasswd", cfg.HtpasswdPath)
		}
		mux.Handle("/metrics/node", metrics)
		if cfg.SelfHost != "" {
			mux.Handle(cfg.SelfHost+"/metrics/node", metrics)
		}
	} else {
		// Not fatal: a box with no htpasswd yet still serves traffic, and the scrape shows up as a
		// down target, which is the signal that matters.
		s.log.Warn("metrics scrape not mounted", "htpasswd", cfg.HtpasswdPath, "err", err)
	}
	return mux, nil
}

// RedirectHandler is the plaintext listener when TLS is configured: health, and a redirect for
// everything else. No ACME challenge location — the fleet certificate is issued on the control
// plane over DNS-01, which is the only thing that works when the shared name resolves to a
// different box depending on where the client asks from.
func (s *Server) RedirectHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.Health)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://"+r.Host+r.URL.RequestURI(), http.StatusMovedPermanently)
	})
	return mux
}

// TLSConfig serves the fleet certificate and reloads it from disk when it changes.
func (s *Server) TLSConfig(cfg config.Config) (*tls.Config, error) {
	loader, err := newCertLoader(cfg.TLSCertPath, cfg.TLSKeyPath, s.log)
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		GetCertificate: loader.GetCertificate,
		MinVersion:     tls.VersionTLS12,
		NextProtos:     []string{"h2", "http/1.1"},
	}, nil
}

// NewHTTPServer builds a server with the timeouts a token stream needs.
//
// WriteTimeout is deliberately zero. It bounds the whole response, and a completion legitimately
// takes minutes to write — any value here would cut long generations at exactly the point they
// become valuable. ReadHeaderTimeout does the work a WriteTimeout is usually reached for: it is
// what stops a connection that sends nothing from holding a slot.
func NewHTTPServer(addr string, handler http.Handler, tlsConfig *tls.Config) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           handler,
		TLSConfig:         tlsConfig,
		ReadHeaderTimeout: 30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
}
