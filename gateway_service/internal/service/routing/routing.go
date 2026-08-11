// Package routing turns a model into an engine. Stickiness, region tiering, the capacity gate and
// least-in-flight all live here; the rule itself is domain.PickRoute and is shared with the ingress
// tier unchanged.
package routing

import (
	"context"
	"log/slog"
	"time"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository"
)

// StickyTTL is how long a caller that names its own session keeps its engine. Client-declared
// affinity, not a fallback, so it is a constant rather than the synthetic knob below.
const StickyTTL = 30 * time.Minute

// Request is what a pick needs. Session is the caller's own hint (header or the body's `user`
// field) and may be blank.
type Request struct {
	Model     string
	Session   string
	MeterID   string
	KeyPrefix string
	// Path is the surface being asked for, checked against the model's modality. An ASR model and
	// a chat model are indistinguishable by name alone.
	Path string
}

// Decision is the pick and everything downstream needs to act on it.
type Decision struct {
	Route     domain.Route
	RequestID string
	Session   string // the session actually pinned; "" when none was used
	// SessionKey is sha256(Session), set only for an ingress route. The gateway's session may be a
	// caller-chosen string that names the tenant; the infra plane keys on the hash instead.
	SessionKey string
}

// EngineURL and RequestID together name the in-flight slot this decision claimed.
func (d Decision) EngineURL() string { return d.Route.EngineURL }

type Service struct {
	routes   repository.Routes
	sessions repository.Sessions
	inFlight repository.InFlight
	health   repository.Health
	log      *slog.Logger

	gatewayID string
	region    string
	// syntheticTTL is how long a caller that names no session is pinned to one engine. 0 = not at
	// all, which balances every such request. A function, not a value: it is the knob most likely
	// to be turned while the fleet is running, and reading it per request is what makes turning it
	// an edit rather than a deploy.
	syntheticTTL func() time.Duration
}

type Options struct {
	GatewayID string
	Region    string
	// SyntheticTTL is read on every pick. Nil means no synthetic session at all.
	SyntheticTTL func() time.Duration
}

func New(store repository.Store, log *slog.Logger, opts Options) *Service {
	return &Service{
		routes: store.Routes, sessions: store.Sessions,
		inFlight: store.InFlight, health: store.Health,
		log:       log,
		gatewayID: opts.GatewayID, region: opts.Region,
		syntheticTTL: opts.SyntheticTTL,
	}
}

// Pick chooses an engine and claims a slot on it. The claim is the caller's obligation to release:
// every path that gets a Decision back must Release it, including on client disconnect.
func (s *Service) Pick(ctx context.Context, req Request) (Decision, error) {
	// A caller that names a session keeps its engine, so its prefix cache stays warm. One that
	// names none is balanced — unless the operator asked for the synthetic session back, which
	// pins a whole key to one engine and is what a single-placement fleet always did.
	session, ttl := req.Session, StickyTTL
	synthetic := time.Duration(0)
	if s.syntheticTTL != nil {
		synthetic = s.syntheticTTL()
	}
	if session == "" && synthetic > 0 {
		session = domain.SHA256Hex(req.MeterID + "|" + req.Model)[:24]
		ttl = synthetic
	}

	table, err := s.routes.Get(ctx, req.Model)
	if err != nil || len(table) == 0 {
		if err != nil {
			s.log.Error("route store unreadable", "model", req.Model, "err", err)
		}
		return Decision{}, domain.Deny(503, "model unavailable")
	}

	// Every row of a model carries the same modality — it is the model's, stamped per row — so the
	// first one answers for the table. Refused here rather than forwarded: the engine would 404 it
	// anyway, and this stage sits above meter, so a wrong-surface call costs nothing and bills
	// nothing.
	if !domain.Serves(table[0].Modality, req.Path) {
		return Decision{}, domain.Deny(404, req.Model+" does not serve "+req.Path)
	}

	var stickyURL string
	if session != "" {
		stickyURL, _ = s.sessions.Engine(ctx, session)
	}
	s.fillInFlight(ctx, table)
	s.markUnhealthy(ctx, table)

	route, status := domain.PickRoute(table, stickyURL, s.region)
	if status == 429 {
		return Decision{}, domain.Deny(429, "every replica of "+req.Model+" is at capacity")
	}
	if status != 200 {
		return Decision{}, domain.Deny(503, "no healthy server for model")
	}

	if session != "" {
		if err := s.sessions.Pin(ctx, session, route.EngineURL, ttl); err != nil {
			// A lost pin costs one cold prefix cache, not a wrong answer.
			s.log.Warn("session pin failed", "session", session, "err", err)
		}
	}

	decision := Decision{
		Route:     route,
		RequestID: domain.BuildRequestID(s.gatewayID, route, req.KeyPrefix),
		Session:   session,
	}
	if route.IsIngress() && session != "" {
		decision.SessionKey = domain.SHA256Hex(session)
	}
	if err := s.inFlight.Claim(ctx, route.EngineURL, decision.RequestID); err != nil {
		// Undercounting an engine biases it toward more traffic; refusing the request over a
		// counter would be worse.
		s.log.Warn("in-flight claim failed", "engine", route.EngineURL, "err", err)
	}
	s.log.Debug("route picked",
		"model", req.Model, "engine", route.EngineURL, "deployment", route.Deployment,
		"kind", route.Kind, "in_flight", route.InFlight, "sticky", stickyURL != "",
		"candidates", len(table), "rid", decision.RequestID)
	return decision, nil
}

// PickReplica is the ingress tier: the same rule with no session synthesis, no region (every
// replica is in this ingress's own VPC) and an already-opaque session key.
func (s *Service) PickReplica(ctx context.Context, model, sessionKey, requestID string) (domain.Route, error) {
	table, err := s.routes.Get(ctx, model)
	if err != nil || len(table) == 0 {
		// no-replica, not unhealthy. The gateway must not eject a whole network over one model
		// that has nowhere to go inside it.
		return domain.Route{}, domain.Deny(503, "no-replica")
	}
	s.fillInFlight(ctx, table)

	var stickyURL string
	if sessionKey != "" {
		stickyURL, _ = s.sessions.Engine(ctx, sessionKey)
	}
	route, status := domain.PickRoute(table, stickyURL, "")
	if status == 429 {
		return domain.Route{}, domain.Deny(429, "at-capacity")
	}
	if status != 200 {
		return domain.Route{}, domain.Deny(503, "no-replica")
	}
	if sessionKey != "" {
		if err := s.sessions.Pin(ctx, sessionKey, route.EngineURL, StickyTTL); err != nil {
			s.log.Warn("replica pin failed", "err", err)
		}
	}
	if err := s.inFlight.Claim(ctx, route.EngineURL, requestID); err != nil {
		s.log.Warn("in-flight claim failed", "engine", route.EngineURL, "err", err)
	}
	return route, nil
}

// Release crosses a finished request off its engine.
func (s *Service) Release(ctx context.Context, engineURL, requestID string) {
	if err := s.inFlight.Release(ctx, engineURL, requestID); err != nil {
		s.log.Warn("in-flight release failed", "engine", engineURL, "rid", requestID, "err", err)
	}
}

// fillInFlight sets each route's InFlight to what is running on its engine right now.
//
// A store failure leaves every count at zero, which degrades to taking the first healthy route.
// Refusing the request instead would 503 a working engine over an unreadable counter.
func (s *Service) fillInFlight(ctx context.Context, table []domain.Route) {
	urls := make([]string, len(table))
	for i, r := range table {
		urls[i] = r.EngineURL
	}
	counts, err := s.inFlight.Counts(ctx, urls)
	if err != nil {
		s.log.Warn("in-flight counts unavailable, falling back to the first healthy route", "err", err)
		return
	}
	for i := range table {
		table[i].InFlight = counts[i]
	}
}

// markUnhealthy flips Healthy off on every route whose target has failed too often in a row.
// PickRoute's existing Healthy check does the rest.
//
// A store failure leaves every route as the control plane pushed it, which is the safe direction:
// ejection is an optimisation on top of a table that is already correct, and refusing to route
// because a health counter is unreadable would turn one broken store into an outage.
func (s *Service) markUnhealthy(ctx context.Context, table []domain.Route) {
	targets := make([]string, len(table))
	for i, r := range table {
		targets[i] = r.EngineURL
	}
	failures, err := s.health.Failures(ctx, targets)
	if err != nil {
		return
	}
	for i := range table {
		if failures[i] >= domain.EjectAfter {
			table[i].Healthy = false
			s.log.Debug("target ejected", "engine", table[i].EngineURL, "failures", failures[i])
		}
	}
}
