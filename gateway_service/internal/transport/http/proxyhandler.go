package http

import (
	"net/http"

	"grove-gateway/internal/transport/http/middleware"
	"grove-gateway/internal/transport/respond"
)

// proxyHandler is the bottom of the chain: every decision has been made above it, so all this does
// is forward to the engine that was picked and record what came back.
func (s *Server) proxyHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		state := middleware.From(r)
		target := state.Decision.EngineURL()
		if target == "" {
			// Nothing picked a route, which means the chain was assembled without one. A config
			// fault, not a caller's.
			s.log.Error("no route on an admitted request", "path", r.URL.Path)
			respond.Error(w, http.StatusInternalServerError, "gateway error")
			return
		}

		outcome := s.proxy.Forward(w, r, target)
		state.UpstreamStatus = outcome.Status
		state.Usage = outcome.Usage
		state.Reason = outcome.Reason
		if outcome.Deployment != "" {
			// An ingress picked the replica and said so. The only way usage reaches a placement
			// this gateway never chose.
			state.Deployment = outcome.Deployment
		}
	})
}

// root is what a bare GET / answers, so a browser aimed at the gateway sees something other than a
// 404.
func root(w http.ResponseWriter, _ *http.Request) {
	respond.JSON(w, map[string]any{
		"status":  "ok",
		"message": "Grove Gateway Service",
		"usage":   "POST /v1/messages or /v1/chat/completions",
	})
}
