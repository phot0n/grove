// Package http is the only place in the binary that imports net/http. Every decision it serves
// belongs to a service below it; this layer does routing, decoding, status codes and nothing else.
package http

import (
	"log/slog"
	"net/http"
	"sync/atomic"

	"grove-gateway/internal/config"
	"grove-gateway/internal/service/admission"
	"grove-gateway/internal/service/catalog"
	"grove-gateway/internal/service/metering"
	"grove-gateway/internal/service/provisioning"
	"grove-gateway/internal/service/routing"
	"grove-gateway/internal/service/transform"
	"grove-gateway/internal/transport/http/middleware"
	"grove-gateway/internal/transport/http/proxy"
	"grove-gateway/internal/transport/respond"
)

// Server holds the services the handlers reach and the two secrets the transport itself checks.
type Server struct {
	admission    *admission.Service
	routing      *routing.Service
	metering     *metering.Service
	catalog      *catalog.Service
	provisioning *provisioning.Service
	proxy        *proxy.Proxy
	log          *slog.Logger

	deps  middleware.Deps
	drain middleware.DrainState
	// chain is swapped in place when the tunables file changes the middleware list, so the mux
	// built at startup keeps serving and only what it dispatches to moves.
	chain swappable

	adminToken string
	isIngress  bool
}

// Services is what New needs. A struct rather than eight positional arguments, so adding one does
// not silently reorder the others.
type Services struct {
	Admission    *admission.Service
	Routing      *routing.Service
	Metering     *metering.Service
	Catalog      *catalog.Service
	Provisioning *provisioning.Service
	Transform    *transform.Chain
	Proxy        *proxy.Proxy
	Drain        middleware.DrainState
	Access       *slog.Logger
	// MaxBodyBytes is read per request, so a reload moves it.
	MaxBodyBytes func() int64
}

func New(cfg config.Config, svc Services, log *slog.Logger) *Server {
	return &Server{
		admission: svc.Admission, routing: svc.Routing, metering: svc.Metering,
		catalog: svc.Catalog, provisioning: svc.Provisioning, proxy: svc.Proxy,
		log: log, drain: svc.Drain,
		deps: middleware.Deps{
			Admission: svc.Admission, Routing: svc.Routing, Metering: svc.Metering,
			Transform: svc.Transform, Drain: svc.Drain, Log: log, Access: svc.Access,
			MaxBodyBytes: svc.MaxBodyBytes, IngressToken: cfg.IngressToken,
		},
		adminToken: cfg.AdminToken,
		isIngress:  cfg.IsIngress(),
	}
}

// DataHandler is the customer-facing surface: /v1/, plus the model list the gateway answers itself.
// The chain resolves here rather than per request, so an unknown middleware name fails at startup —
// a misspelt `quota` that merely warned would silently stop enforcing the budget.
func (s *Server) DataHandler(chain []string) (http.Handler, error) {
	if err := s.SetChain(chain); err != nil {
		return nil, err
	}
	mux := http.NewServeMux()
	if s.isIngress {
		// An ingress forwards whatever the gateway sends and reads none of it.
		mux.Handle("/", &s.chain)
		return mux, nil
	}

	// Exact match, so it wins over the /v1/ proxy and is never forwarded to an engine — an engine
	// only knows its own model. Outside the data chain: it needs no route and claims no slot.
	mux.HandleFunc("GET /v1/models", s.handleModels)
	mux.Handle("/v1/", &s.chain)
	mux.HandleFunc("GET /{$}", root)
	return mux, nil
}

// SetChain builds the data chain and swaps it in. Returning an error leaves the running chain
// exactly as it was — which is what makes a bad middleware name in the tunables file a log line
// rather than an outage.
func (s *Server) SetChain(chain []string) error {
	if len(chain) == 0 {
		chain = middleware.GatewayChain
		if s.isIngress {
			chain = middleware.IngressChain
		}
	}
	wrap, err := middleware.Chain(s.deps, chain)
	if err != nil {
		return err
	}
	s.chain.set(wrap(s.proxyHandler()))
	return nil
}

// swappable dispatches to whatever handler is current. One atomic load per request, which is the
// cost of not having to rebuild the mux — or drop a connection — when the chain changes.
type swappable struct {
	current atomic.Pointer[http.Handler]
}

func (s *swappable) set(h http.Handler) { s.current.Store(&h) }

func (s *swappable) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	handler := s.current.Load()
	if handler == nil {
		respond.Error(w, http.StatusServiceUnavailable, "gateway not ready")
		return
	}
	(*handler).ServeHTTP(w, r)
}

// AdminHandler is the control plane's surface. Mounted on this box's OWN name, never on the shared
// one: a push has to reach one gateway, and Gateway Host names them all.
func (s *Server) AdminHandler() http.Handler {
	mux := http.NewServeMux()
	// The replica table is the one thing both planes hold, so both take this push.
	mux.HandleFunc("/grove-admin/routes", adminAuth(s.adminToken, s.handleAdminRoutes))
	if s.isIngress {
		return mux
	}
	mux.HandleFunc("/grove-admin/keys", adminAuth(s.adminToken, s.handleAdminKeys))
	mux.HandleFunc("/grove-admin/users", adminAuth(s.adminToken, s.handleAdminUsers))
	mux.HandleFunc("/grove-admin/groups", adminAuth(s.adminToken, s.handleAdminGroups))
	mux.HandleFunc("/grove-admin/usage", adminAuth(s.adminToken, s.handleAdminUsage))
	return mux
}

// Health answers the liveness check every tier in front of this box asks. It reports 503 the moment
// a drain starts, which is what pulls the box out of rotation BEFORE its socket goes anywhere.
func (s *Server) Health(w http.ResponseWriter, _ *http.Request) {
	if s.drain != nil && s.drain.Draining() {
		respond.Error(w, http.StatusServiceUnavailable, "draining")
		return
	}
	_, _ = w.Write([]byte("ok\n"))
}
