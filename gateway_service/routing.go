package main

// Route is one placement of a model: the vLLM instance's URL plus the internal
// key to reach it. Mirrors an entry of Redis deploy:<model> (§8A.D).
type Route struct {
	EngineURL   string  `json:"engine_url"`
	InternalKey string  `json:"internal_key"`
	Healthy     bool    `json:"healthy"`
	Region      string  `json:"region"`
	Load        float64 `json:"load"`
	Server      string  `json:"server"` // inference-server / pod id — the request-id's target part
}

// pickRoute implements Tier-1 selection with session stickiness (§6 jobs 5-6):
// among the healthy routes for a model, reuse the sticky one if it's still
// healthy, else take the least-loaded. The model is invariant — callers only
// ever pass routes that host the requested model, so failover keeps the model
// and swaps the place. Returns ok=false when no healthy route exists → 503.
func pickRoute(routes []Route, stickyURL string) (Route, bool) {
	var healthy []Route
	for _, r := range routes {
		if r.Healthy {
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
		if r.Load < best.Load {
			best = r
		}
	}
	return best, true
}
