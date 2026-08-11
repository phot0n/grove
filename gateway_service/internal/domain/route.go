package domain

// Route is one placement of a model: the vLLM instance's URL plus the internal
// key to reach it. Mirrors an entry of Redis deploy:<model> (§8A.D).
type Route struct {
	EngineURL   string `json:"engine_url"`
	InternalKey string `json:"internal_key"`
	Healthy     bool   `json:"healthy"`
	Region      string `json:"region"`
	// Requests admitted to this engine and not yet metered, counted at decide time. Never pushed —
	// the control plane has no view of what is running right now.
	InFlight int `json:"-"`
	// The engine's --max-num-seqs: what it runs concurrently before vLLM starts queueing. 0 =
	// unset on the placement, which means no cap here rather than a guess at vLLM's default.
	Capacity int `json:"capacity"`
	// Model Deployment / pod id — which placement this is, and the request-id's target part.
	// One box can serve the same model from two deployments, so Server alone names neither.
	// Empty on a route pushed before this field existed; BuildRequestID falls back.
	Deployment string `json:"deployment"`
	Server     string `json:"server"` // inference-server / pod id — which box it is on
	// "ingress" when this row is an Ingress Server that will pick a replica of its own, "direct"
	// (or empty, on a route pushed before this field existed) when it is an engine to dial.
	Kind string `json:"kind"`
	// Which OpenAI surface the model answers on — the control plane's Model.modality, stamped on
	// every row of the model because deploy:<model> is the only thing pushed per model. Blank on a
	// route pushed before this field existed, which reads as unrestricted.
	Modality string `json:"modality"`
}

// IsIngress reports whether this row hands off to an ingress rather than naming an engine.
// Empty Kind is direct, which is what every route pushed before the split was.
func (r Route) IsIngress() bool { return r.Kind == "ingress" }

// HasRoom reports whether this engine can take another request. A placement with no
// --max-num-seqs set has no number to hold it to, so it is never held back.
func (r Route) HasRoom() bool { return r.Capacity <= 0 || r.InFlight < r.Capacity }

// nearest narrows to routes in this gateway's region when there are any, so stickiness and
// least-in-flight run inside the winning tier. Two tiers only — ranking remote regions is precision
// nobody can feel. Blank Region reads as remote; nothing local falls through, since far beats 503.
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

// PickRoute reuses the sticky route when healthy and with room, else fewest in-flight. 200 admits,
// 503 means nowhere healthy, 429 means every replica is at capacity — only 429 says retry. A tie
// keeps the first; the caller's claim breaks it next time.
func PickRoute(routes []Route, stickyURL, region string) (Route, int) {
	var healthy []Route
	for _, r := range routes {
		// An empty engine URL is unroutable, whatever the pusher claimed: the proxy would have no
		// target and the caller would see a 500 instead of the 503 that a model with nowhere to go
		// actually means.
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
		if r.HasRoom() {
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
