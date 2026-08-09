package main

// Route is one placement of a model: the vLLM instance's URL plus the internal
// key to reach it. Mirrors an entry of Redis deploy:<model> (§8A.D).
type Route struct {
	EngineURL   string `json:"engine_url"`
	InternalKey string `json:"internal_key"`
	Healthy     bool   `json:"healthy"`
	Region      string `json:"region"`
	// Requests admitted to this engine and not yet metered, counted at decide time (inflight.go).
	// Never pushed — the control plane has no view of what is running right now.
	InFlight int `json:"-"`
	// The engine's --max-num-seqs: what it runs concurrently before vLLM starts queueing. 0 =
	// unset on the placement, which means no cap here rather than a guess at vLLM's default.
	Capacity int `json:"capacity"`
	// Model Deployment / pod id — which placement this is, and the request-id's target part.
	// One box can serve the same model from two deployments, so Server alone names neither.
	// Empty on a route pushed before this field existed; buildRequestID falls back.
	Deployment string `json:"deployment"`
	Server     string `json:"server"` // inference-server / pod id — which box it is on
	// "ingress" when this row is an Ingress Server that will pick a replica of its own, "direct"
	// (or empty, on a route pushed before this field existed) when it is an engine to dial.
	// Lua reads it to decide whether to send the ingress headers; nothing else branches on it.
	Kind string `json:"kind"`
}

// isIngress reports whether this row hands off to an ingress rather than naming an engine.
// Empty Kind is direct, which is what every route pushed before the split was.
func (r Route) isIngress() bool { return r.Kind == "ingress" }

// hasRoom reports whether this engine can take another request. A placement with no
// --max-num-seqs set has no number to hold it to, so it is never held back.
func (r Route) hasRoom() bool { return r.Capacity <= 0 || r.InFlight < r.Capacity }

// nearest narrows a model's routes to the ones in this gateway's own region, when there are any.
// Everything after it — stickiness, the capacity gate, least-in-flight — then runs inside the
// winning tier, so an idle replica on another continent never wins on in-flight count alone.
//
// Two tiers and no further ordering: a cross-region hop costs so much more than the difference
// between two remote regions that ranking the far ones would be precision nobody can feel. A
// route with no Region is treated as remote — every route carried one before this mattered, and
// the safe reading of "unknown" is "not local".
//
// Falls through unchanged when the gateway has no region of its own, or when nothing local is
// left: a far replica beats a 503.
func nearest(routes []Route, region string) []Route {
	if region == "" {
		return routes
	}
	var local []Route
	for _, r := range routes {
		if r.Region == region {
			local = append(local, r)
		}
	}
	if len(local) == 0 {
		return routes
	}
	return local
}

// pickRoute implements Tier-1 selection with session stickiness (§6 jobs 5-6): among the healthy
// routes for a model, reuse the sticky one if it is still healthy and has room, else take the one
// with the fewest requests in flight. The model is invariant — callers only ever pass routes that
// host the requested model, so failover keeps the model and swaps the place.
//
// Returns the status the caller should answer with: 200 to admit, 503 when the model has nowhere
// healthy to go, and 429 when every healthy replica is already at --max-num-seqs. The two are
// different answers — a 503 says the model is down, a 429 says come back shortly — and only the
// second is the client's cue to back off rather than to page someone.
//
// A tie keeps the first, which is all two idle engines need to alternate: the caller claims the
// engine it was given, so the next request no longer sees a tie.
func pickRoute(routes []Route, stickyURL, region string) (Route, int) {
	var healthy []Route
	for _, r := range routes {
		// An empty engine URL is unroutable, whatever the pusher claimed: Lua would
		// hand nginx a bare path as proxy_pass and the caller would see a 500 instead
		// of the 503 that a model with nowhere to go actually means.
		if r.Healthy && r.EngineURL != "" {
			healthy = append(healthy, r)
		}
	}
	if len(healthy) == 0 {
		return Route{}, 503
	}
	healthy = nearest(healthy, region)

	var free []Route
	for _, r := range healthy {
		if r.hasRoom() {
			free = append(free, r)
		}
	}
	if len(free) == 0 {
		return Route{}, 429
	}

	// Stickiness loses to capacity: a warm prefix cache is not worth queueing behind a full
	// engine when a replica is idle.
	if stickyURL != "" {
		for _, r := range free {
			if r.EngineURL == stickyURL {
				return r, 200
			}
		}
	}
	best := free[0]
	for _, r := range free[1:] {
		if r.InFlight < best.InFlight {
			best = r
		}
	}
	return best, 200
}
