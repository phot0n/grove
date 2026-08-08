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
	// Model Deployment / pod id — which placement this is, and the request-id's target part.
	// One box can serve the same model from two deployments, so Server alone names neither.
	// Empty on a route pushed before this field existed; buildRequestID falls back.
	Deployment string `json:"deployment"`
	Server     string `json:"server"` // inference-server / pod id — which box it is on
}

// pickRoute implements Tier-1 selection with session stickiness (§6 jobs 5-6):
// among the healthy routes for a model, reuse the sticky one if it's still
// healthy, else take the one with the fewest requests in flight. The model is
// invariant — callers only ever pass routes that host the requested model, so
// failover keeps the model and swaps the place. Returns ok=false when no healthy
// route exists → 503.
//
// A tie keeps the first, which is all two idle engines need to alternate: the caller claims the
// engine it was given, so the next request no longer sees a tie.
func pickRoute(routes []Route, stickyURL string) (Route, bool) {
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
		return Route{}, false
	}
	if stickyURL != "" {
		for _, r := range healthy {
			if r.EngineURL == stickyURL {
				return r, true
			}
		}
	}
	best := healthy[0]
	for _, r := range healthy[1:] {
		if r.InFlight < best.InFlight {
			best = r
		}
	}
	return best, true
}
