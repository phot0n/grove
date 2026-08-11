// Package proxy forwards an admitted request to the engine that was picked for it, and reads the
// usage frame out of the response on the way back without touching a byte of it.
package proxy

import (
	"crypto/tls"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Outcome is what the proxy learned, for metering and passive ejection.
type Outcome struct {
	Status int    // the upstream's status; 0 means the hop never produced one
	Usage  string // the captured usage line, empty if the response carried none
	// Deployment is the placement an ingress chose, off its response header. On a direct route the
	// gateway already knows; this is the only way usage reaches a placement it never picked.
	Deployment string
	// Reason is an ingress's X-Grove-Reason. A no-replica 503 means the ingress answered correctly
	// and must not count against it.
	Reason string
}

// Options are the dials that used to be nginx directives.
type Options struct {
	// ReadTimeout bounds a whole generation. Long: a large completion legitimately takes minutes.
	ReadTimeout time.Duration
	DialTimeout time.Duration
	// VerifyUpstream turns on certificate verification for engine and ingress hops. Per target here,
	// so it is a fleet default rather than the ceiling nginx's per-location directive imposed.
	VerifyUpstream bool
}

func (o Options) withDefaults() Options {
	if o.ReadTimeout <= 0 {
		o.ReadTimeout = 600 * time.Second
	}
	if o.DialTimeout <= 0 {
		o.DialTimeout = 10 * time.Second
	}
	return o
}

// Proxy holds the transports. One per target host rather than one for the fleet: it is what makes
// verification a property of the hop instead of a property of the whole listener.
type Proxy struct {
	log *slog.Logger

	mu         sync.RWMutex
	opts       Options
	transports map[string]http.RoundTripper
}

func New(opts Options, log *slog.Logger) *Proxy {
	return &Proxy{opts: opts.withDefaults(), log: log, transports: map[string]http.RoundTripper{}}
}

// Reconfigure replaces the options and DROPS the transport pool. Verification and the header
// timeout are baked in at build time, so keeping it would leave engines already dialled on the old
// setting and new ones on the new. Live connections finish on the old transport, then it is garbage.
func (p *Proxy) Reconfigure(opts Options) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.opts = opts.withDefaults()
	p.transports = map[string]http.RoundTripper{}
}

// Forward proxies to target (a base URL; the client's path is appended) and reports what the hop
// did. The Outcome is filled even on a dial failure — a hop with no status is itself the signal
// that ejects a dead engine.
func (p *Proxy) Forward(w http.ResponseWriter, r *http.Request, target string) Outcome {
	base, err := url.Parse(target)
	if err != nil || base.Host == "" {
		p.log.Error("unroutable target", "target", target, "err", err)
		return Outcome{}
	}

	outcome := &Outcome{}
	var tee *usageTee

	reverse := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme = base.Scheme
			pr.Out.URL.Host = base.Host
			pr.Out.URL.Path = singleJoin(base.Path, pr.In.URL.Path)
			pr.Out.URL.RawQuery = pr.In.URL.RawQuery
			// SNI and virtual hosts: the target's name, not the one the client asked us for.
			pr.Out.Host = base.Host
			// ReverseProxy drops inbound X-Forwarded-* whenever Rewrite is set, so a rewrite cannot
			// carry a spoofed header. Ours are not the client's — upstreamauth just wrote them from
			// the peer address — so they go back explicitly, not via SetXForwarded, which appends.
			for _, header := range []string{"X-Forwarded-For", "X-Real-IP", "X-Forwarded-Proto"} {
				if value := pr.In.Header.Get(header); value != "" {
					pr.Out.Header.Set(header, value)
				}
			}
		},
		Transport: p.transportFor(base),
		// -1 flushes every write immediately, which is what streams a token the moment it arrives.
		// Any positive interval would batch an SSE stream into chunks and make the response feel
		// slower than the engine actually is.
		FlushInterval: -1,
		ModifyResponse: func(resp *http.Response) error {
			outcome.Status = resp.StatusCode
			outcome.Deployment = resp.Header.Get("X-Grove-Engine")
			outcome.Reason = resp.Header.Get("X-Grove-Reason")
			// A 101 hands the connection to ReverseProxy, which needs the body to stay an
			// io.ReadWriteCloser to write back to the engine. The tee is read-only and would fail
			// the handshake — and a hijacked stream has no usage frame to scrape anyway.
			if resp.StatusCode == http.StatusSwitchingProtocols {
				return nil
			}
			tee = newUsageTee(resp.Body)
			resp.Body = tee
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// Status stays 0, which is what marks the hop failed: the connection never got far
			// enough to have one. A client that hung up lands here too, and is not worth
			// distinguishing against a threshold of three consecutive failures.
			p.log.Warn("upstream hop failed", "target", target, "err", err)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			_, _ = w.Write([]byte(`{"error":{"message":"upstream unavailable","type":"grove_gateway"}}` + "\n"))
		},
	}

	reverse.ServeHTTP(w, r)
	if tee != nil {
		outcome.Usage = tee.Usage()
	}
	return *outcome
}

// transportFor keeps one transport per target host, so connections are pooled per engine and the
// TLS settings are the hop's own.
func (p *Proxy) transportFor(base *url.URL) http.RoundTripper {
	host := base.Scheme + "://" + base.Host

	p.mu.RLock()
	existing, ok := p.transports[host]
	p.mu.RUnlock()
	if ok {
		return existing
	}

	p.mu.Lock()
	defer p.mu.Unlock()
	if existing, ok := p.transports[host]; ok {
		return existing
	}
	opts := p.opts
	transport := &http.Transport{
		DialContext:           (&net.Dialer{Timeout: opts.DialTimeout, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:   10 * time.Second,
		ResponseHeaderTimeout: opts.ReadTimeout,
		IdleConnTimeout:       90 * time.Second,
		MaxIdleConnsPerHost:   64,
		// Streaming responses must not be buffered on the way in either.
		DisableCompression: true,
		ForceAttemptHTTP2:  true,
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: !opts.VerifyUpstream, //nolint:gosec // see Options.VerifyUpstream
			MinVersion:         tls.VersionTLS12,
		},
	}
	p.transports[host] = transport
	return transport
}

// singleJoin appends the client's path to the target's base without doubling or dropping a slash.
// The base carries the engine's location prefix (https://box/e/md-00007), so this is what makes
// /v1/chat/completions land on /e/md-00007/v1/chat/completions.
func singleJoin(base, requested string) string {
	switch {
	case base == "" || base == "/":
		return requested
	case strings.HasSuffix(base, "/") && strings.HasPrefix(requested, "/"):
		return base + strings.TrimPrefix(requested, "/")
	case !strings.HasSuffix(base, "/") && !strings.HasPrefix(requested, "/"):
		return base + "/" + requested
	default:
		return base + requested
	}
}
