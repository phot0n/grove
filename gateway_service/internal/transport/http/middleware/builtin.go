package middleware

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/service/metering"
	"grove-gateway/internal/service/routing"
	"grove-gateway/internal/service/transform"
	"grove-gateway/internal/transport/respond"
)

func init() {
	Register("recover", newRecover)
	Register("accesslog", newAccessLog)
	Register("drain", newDrain)
	Register("auth", newAuth)
	Register("quota", newQuota)
	Register("body", newBody)
	Register("modelaccess", newModelAccess)
	Register("route", newRoute)
	Register("meter", newMeter)
	Register("transform", newTransform)
	Register("upstreamauth", newUpstreamAuth)
	Register("ingressauth", newIngressAuth)
	Register("pick", newPick)
}

// recover answers a panic instead of dropping the connection. Outermost, so it covers every stage
// below including the ones that write the response.
func newRecover(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if p := recover(); p != nil {
					// ErrAbortHandler is net/http's own way of saying "this connection is going
					// away"; logging it as a crash would fill the log with client disconnects.
					if errors.Is(p.(error), http.ErrAbortHandler) {
						panic(p)
					}
					deps.Log.Error("panic serving request", "path", r.URL.Path, "panic", p)
					respond.Error(w, http.StatusInternalServerError, "gateway error")
				}
			}()
			next.ServeHTTP(w, r)
		})
	}, nil
}

// accesslog times the request and writes the one durable line per request. It also creates the
// State, being the outermost stage that needs one.
func newAccessLog(deps Deps) (Middleware, error) {
	access := deps.Access
	if access == nil {
		access = deps.Log
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			r, state := newState(r)
			recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}

			next.ServeHTTP(recorder, r)

			access.LogAttrs(r.Context(), slog.LevelInfo, "access",
				slog.String("remote", clientIP(r)),
				slog.String("method", r.Method),
				slog.String("path", r.URL.Path),
				slog.Int("status", recorder.status),
				slog.Int64("bytes", recorder.written),
				slog.Float64("rt", time.Since(state.Started).Seconds()),
				slog.String("key", or(state.Identity.Prefix(), "-")),
				slog.String("model", or(state.Model, "-")),
				slog.String("rid", or(state.Decision.RequestID, "-")),
				slog.String("upstream", or(state.Decision.EngineURL(), "-")),
				slog.String("deployment", or(state.Decision.Route.Deployment, "-")),
				slog.String("engine", or(state.Deployment, "-")),
				slog.Int("upstream_status", state.UpstreamStatus),
				slog.String("reason", or(state.DeniedReason, state.Reason)),
			)
		})
	}, nil
}

// drain answers while the process is shutting down. In-flight requests are past this stage and stay
// past it; only a new one on an already-open connection lands here, and it gets a real message with
// a retry hint rather than a reset.
func newDrain(deps Deps) (Middleware, error) {
	if deps.Drain == nil {
		return passthrough, nil
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !deps.Drain.Draining() {
				next.ServeHTTP(w, r)
				return
			}
			state := From(r)
			state.Denied, state.DeniedReason = http.StatusServiceUnavailable, "draining"
			w.Header().Set("Retry-After", "5")
			w.Header().Set("Connection", "close")
			respond.Error(w, http.StatusServiceUnavailable, "gateway is restarting, retry shortly")
		})
	}, nil
}

// auth resolves the caller: bearer → key → user → group, once, into the State.
func newAuth(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			identity, err := deps.Admission.Identify(r.Context(), r.Header.Get("Authorization"))
			if err != nil {
				deny(w, r, err)
				return
			}
			From(r).Identity = identity
			next.ServeHTTP(w, r)
		})
	}, nil
}

// quota honours the monthly token budget the control plane pushed. The gateway keeps no counters of
// its own — it reads a flag someone else computed.
func newQuota(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if From(r).Identity.User.Limited {
				deny(w, r, domain.Deny(http.StatusTooManyRequests, "monthly token quota exhausted"))
				return
			}
			next.ServeHTTP(w, r)
		})
	}, nil
}

// body reads and decodes the request body, bounded. The model and the session hint come out of it,
// and every stage below reads them from the State rather than parsing again.
func newBody(deps Deps) (Middleware, error) {
	limit := func() int64 {
		if deps.MaxBodyBytes == nil {
			return 32 << 20
		}
		if configured := deps.MaxBodyBytes(); configured > 0 {
			return configured
		}
		return 32 << 20
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			maxBytes := limit()

			// A protocol upgrade — a realtime WebSocket session — carries no body at all, so the
			// model comes from the query string, which is where the OpenAI realtime API puts it.
			// Nothing here reads or restores the body: stamping Content-Length on an upgrade would
			// break the handshake before it reached an engine.
			if isUpgrade(r) {
				query := r.URL.Query()
				state.Model = strings.TrimSpace(query.Get("model"))
				state.Session = strings.TrimSpace(query.Get("user"))
				applySessionHeader(r, state)
				next.ServeHTTP(w, r)
				return
			}

			// A multipart body gives up its fields without being materialised and is forwarded as it
			// arrived. It never becomes a transform.Body, so the transform stage below skips it
			// rather than re-encoding a form as JSON.
			if boundary := multipartBoundary(r); boundary != "" {
				if r.ContentLength > maxBytes {
					deny(w, r, domain.Deny(http.StatusRequestEntityTooLarge, "request body too large"))
					return
				}
				model, session, err := readMultipart(w, r, boundary, maxBytes, deps.Log)
				if err != nil {
					denyUnreadableBody(w, r, err)
					return
				}
				state.Model, state.Session = model, session
				applySessionHeader(r, state)
				next.ServeHTTP(w, r)
				return
			}

			raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBytes))
			if err != nil {
				denyUnreadableBody(w, r, err)
				return
			}
			_ = r.Body.Close()
			state.RawBody = raw

			// A body that is not a JSON object is not an error here: some /v1 endpoints take none
			// at all, and the engine is the right place to reject a malformed one.
			var decoded transform.Body
			if len(raw) > 0 && json.Unmarshal(raw, &decoded) == nil {
				state.Body = decoded
				state.Model = stringField(decoded, "model")
				state.Session = stringField(decoded, "user")
			}
			applySessionHeader(r, state)
			restoreBody(r, raw)
			next.ServeHTTP(w, r)
		})
	}, nil
}

// modelaccess is the grant check: the same decision /v1/models filters its list with.
func newModelAccess(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			if err := deps.Admission.Authorize(state.Identity, state.Model); err != nil {
				deny(w, r, err)
				return
			}
			next.ServeHTTP(w, r)
		})
	}, nil
}

// route picks an engine and claims an in-flight slot on it. Everything below must give that slot
// back, which is meter's job and why meter sits directly under this.
func newRoute(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			decision, err := deps.Routing.Pick(r.Context(), routing.Request{
				Model:     state.Model,
				Session:   state.Session,
				MeterID:   state.Identity.MeterID,
				KeyPrefix: state.Identity.Prefix(),
				Path:      r.URL.Path,
			})
			if err != nil {
				deny(w, r, err)
				return
			}
			state.Decision = decision

			// Canonical, overriding any client-supplied value: vLLM adopts X-Request-Id as its own
			// request id and OpenAI-aware tooling reads it back.
			r.Header.Set("X-Request-Id", decision.RequestID)
			w.Header().Set("X-Request-Id", decision.RequestID)
			next.ServeHTTP(w, r)
		})
	}, nil
}

// meter releases the slot and records what the request cost. Deferred, so it runs on a panic, a
// client disconnect and a dead upstream alike — the cases a metering call placed after the proxy
// would miss, and the ones that leave an engine counted as busy.
func newMeter(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			defer func() {
				// The request's own context is cancelled the moment the client hangs up, which is
				// exactly when this matters most. context.WithoutCancel keeps the store call alive.
				ctx := withoutCancel(r.Context())
				deps.Routing.Release(ctx, state.Decision.EngineURL(), state.Decision.RequestID)
				deps.Metering.Record(ctx, metering.Report{
					Prefix:         state.Identity.Prefix(),
					Model:          state.Model,
					Deployment:     or(state.Deployment, state.Decision.Route.Deployment),
					Usage:          state.Usage,
					Target:         state.Decision.EngineURL(),
					UpstreamStatus: statusText(state.UpstreamStatus),
					Reason:         state.Reason,
				})
			}()
			next.ServeHTTP(w, r)
		})
	}, nil
}

// transform runs the registered body rewrites and re-encodes only if one of them changed something.
func newTransform(deps Deps) (Middleware, error) {
	if deps.Transform == nil {
		return passthrough, nil
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			if state.Body == nil {
				next.ServeHTTP(w, r)
				return
			}
			changed, err := deps.Transform.Apply(transform.Context{
				Path:     r.URL.Path,
				Priority: state.Identity.Priority(),
			}, state.Body)
			if err != nil {
				deps.Log.Error("request transform failed", "path", r.URL.Path, "err", err)
				deny(w, r, domain.Deny(http.StatusInternalServerError, "gateway error"))
				return
			}
			if changed {
				encoded, err := json.Marshal(state.Body)
				if err != nil {
					deny(w, r, domain.Deny(http.StatusInternalServerError, "gateway error"))
					return
				}
				restoreBody(r, encoded)
			}
			next.ServeHTTP(w, r)
		})
	}, nil
}

// upstreamauth swaps the client's key for the target's own and sets the headers the hop needs. The
// client's credential never reaches an engine.
func newUpstreamAuth(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			route := state.Decision.Route
			if route.InternalKey != "" {
				r.Header.Set("Authorization", "Bearer "+route.InternalKey)
			}
			// This is the edge, so a client-sent forwarding header is a claim, not a fact.
			// Overwritten rather than appended: the appending form leaves the caller's entries in
			// front of ours.
			r.Header.Set("X-Forwarded-For", clientIP(r))
			r.Header.Set("X-Real-IP", clientIP(r))

			if route.IsIngress() {
				// An ingress reads no body, so the model it would otherwise have parsed goes as a
				// header — and the session key with it, hashed, because it is the one tier that
				// must not learn whose request this is.
				r.Header.Set("X-Grove-Model", state.Model)
				r.Header.Set("X-Grove-Session-Key", state.Decision.SessionKey)
			} else {
				// A direct route reaches vLLM, which adopts X-Request-Id and nothing else.
				// Clearing these keeps a client-supplied value from reaching an engine that would
				// only ignore it.
				r.Header.Del("X-Grove-Model")
				r.Header.Del("X-Grove-Session-Key")
			}
			next.ServeHTTP(w, r)
		})
	}, nil
}

// ingressauth proves the caller is a gateway. Third layer on a hop that already carries verified
// server TLS and a firewall — and the only one this process can check itself.
func newIngressAuth(deps Deps) (Middleware, error) {
	want := []byte(deps.IngressToken)
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			got := []byte(domain.Bearer(r.Header.Get("Authorization")))
			// A blank configured token refuses everything rather than waving callers through: that
			// is what a misrendered env file looks like, and it would open every engine in the VPC.
			if len(want) == 0 || subtle.ConstantTimeCompare(got, want) != 1 {
				failIngress(w, r, http.StatusUnauthorized, "not-a-gateway")
				return
			}
			next.ServeHTTP(w, r)
		})
	}, nil
}

// pick is the ingress's routing stage: model and session key off headers the gateway set, no body
// read at all. It also releases, because an ingress has no metering to hang that on.
func newPick(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)
			state.Model = r.Header.Get("X-Grove-Model")
			if state.Model == "" {
				failIngress(w, r, http.StatusBadRequest, "no-model")
				return
			}
			sessionKey := r.Header.Get("X-Grove-Session-Key")
			requestID := r.Header.Get("X-Request-Id")

			route, err := deps.Routing.PickReplica(r.Context(), state.Model, sessionKey, requestID)
			if err != nil {
				var denial domain.Denial
				if !errors.As(err, &denial) {
					denial = domain.Deny(http.StatusServiceUnavailable, "no-replica")
				}
				failIngress(w, r, denial.Status, denial.Reason)
				return
			}
			state.Decision = routing.Decision{Route: route, RequestID: requestID}

			// Stamped before the request leaves, so it is set whatever status the engine comes back
			// with. The gateway reads it off the response to attribute usage to a placement it
			// never chose.
			w.Header().Set("X-Grove-Engine", or(route.Deployment, "-"))
			defer func() {
				deps.Routing.Release(withoutCancel(r.Context()), route.EngineURL, requestID)
			}()
			next.ServeHTTP(w, r)
		})
	}, nil
}

// failIngress answers with a NAMED reason. The gateway ejects a whole network on a connection
// failure or a 502/504 and must NOT eject one on a no-replica 503, or a single unplaced model takes
// the network out of rotation for every other model on it.
func failIngress(w http.ResponseWriter, r *http.Request, status int, reason string) {
	state := From(r)
	state.Denied, state.DeniedReason = status, reason
	w.Header().Set("X-Grove-Reason", reason)
	respond.Status(w, status, map[string]any{
		"error": map[string]any{"message": reason, "type": "grove_ingress"},
	})
}

func deny(w http.ResponseWriter, r *http.Request, err error) {
	var denial domain.Denial
	if errors.As(err, &denial) {
		state := From(r)
		state.Denied, state.DeniedReason = denial.Status, denial.Reason
	}
	respond.Denial(w, err)
}

func passthrough(next http.Handler) http.Handler { return next }

// restoreBody puts a fully-read body back on the request so the proxy can forward it.
// isUpgrade reports whether the client asked to switch protocols. Connection is a comma list and
// both header values are case-insensitive, so neither can be compared directly.
func isUpgrade(r *http.Request) bool {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		return false
	}
	for _, value := range r.Header.Values("Connection") {
		for _, token := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return true
			}
		}
	}
	return false
}

func denyUnreadableBody(w http.ResponseWriter, r *http.Request, err error) {
	var tooLarge *http.MaxBytesError
	if errors.As(err, &tooLarge) {
		deny(w, r, domain.Deny(http.StatusRequestEntityTooLarge, "request body too large"))
		return
	}
	deny(w, r, domain.Deny(http.StatusBadRequest, "could not read request body"))
}

// applySessionHeader lets an explicit header beat the body's `user`, which is a best-effort hint.
func applySessionHeader(r *http.Request, state *State) {
	if header := strings.TrimSpace(r.Header.Get("X-Grove-Session")); header != "" {
		state.Session = header
	}
}

func restoreBody(r *http.Request, raw []byte) {
	r.Body = io.NopCloser(bytes.NewReader(raw))
	r.ContentLength = int64(len(raw))
	// A stale Content-Length header would contradict the body a transform just resized.
	r.Header.Set("Content-Length", itoa(len(raw)))
}

func stringField(body transform.Body, name string) string {
	raw, ok := body[name]
	if !ok {
		return ""
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return ""
	}
	return value
}

// statusRecorder remembers what was answered, for the access line.
type statusRecorder struct {
	http.ResponseWriter
	status  int
	written int64
	wrote   bool
}

func (s *statusRecorder) WriteHeader(status int) {
	if !s.wrote {
		s.status, s.wrote = status, true
	}
	s.ResponseWriter.WriteHeader(status)
}

func (s *statusRecorder) Write(p []byte) (int, error) {
	s.wrote = true
	n, err := s.ResponseWriter.Write(p)
	s.written += int64(n)
	return n, err
}

// Unwrap lets net/http find the underlying writer for Flush and Hijack. Without it, wrapping the
// writer would turn every streaming response into a buffered one.
func (s *statusRecorder) Unwrap() http.ResponseWriter { return s.ResponseWriter }
