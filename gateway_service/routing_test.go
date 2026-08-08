package main

import "testing"

func TestPickRouteFewestInFlight(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 9},
		{EngineURL: "b", Healthy: true, InFlight: 2},
		{EngineURL: "c", Healthy: true, InFlight: 5},
	}
	r, ok := pickRoute(routes, "")
	if !ok || r.EngineURL != "b" {
		t.Fatalf("want fewest-in-flight b, got %q ok=%v", r.EngineURL, ok)
	}
}

func TestPickRouteStickyHonored(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 9},
		{EngineURL: "b", Healthy: true, InFlight: 2},
	}
	// Sticky to the busier "a" — stickiness wins over load.
	r, ok := pickRoute(routes, "a")
	if !ok || r.EngineURL != "a" {
		t.Fatalf("want sticky a, got %q ok=%v", r.EngineURL, ok)
	}
}

func TestPickRouteStickyUnhealthyFailsOver(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: false, InFlight: 1}, // pinned server gone
		{EngineURL: "b", Healthy: true, InFlight: 5},
	}
	// Sticky target unhealthy → re-pick among remaining healthy (same model).
	r, ok := pickRoute(routes, "a")
	if !ok || r.EngineURL != "b" {
		t.Fatalf("want failover to b, got %q ok=%v", r.EngineURL, ok)
	}
}

func TestPickRouteNoneHealthy(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: false},
		{EngineURL: "b", Healthy: false},
	}
	if _, ok := pickRoute(routes, ""); ok {
		t.Fatal("expected ok=false when no healthy route (→ 503)")
	}
}

func TestPickRouteSkipsEmptyEngineURL(t *testing.T) {
	routes := []Route{
		{EngineURL: "", Healthy: true, InFlight: 1}, // pushed without a derived engine_url
		{EngineURL: "b", Healthy: true, InFlight: 9},
	}
	if r, ok := pickRoute(routes, ""); !ok || r.EngineURL != "b" {
		t.Fatalf("want b despite higher load, got %q ok=%v", r.EngineURL, ok)
	}
	if _, ok := pickRoute(routes[:1], ""); ok {
		t.Fatal("expected ok=false when the only route has no engine URL (→ 503, not 500)")
	}
}

func TestPickRouteEmpty(t *testing.T) {
	if _, ok := pickRoute(nil, "x"); ok {
		t.Fatal("expected ok=false for empty routes")
	}
}

func TestPickRouteCarriesTheDeployment(t *testing.T) {
	// Two deployments of one model on one box: identical Server, so only Deployment tells them
	// apart — which is why the request id and the access line are built from it.
	routes := []Route{
		{EngineURL: "https://box/e/md-00007", Deployment: "MD-00007", Server: "inf-a", Healthy: true},
		{EngineURL: "https://box/e/md-00008", Deployment: "MD-00008", Server: "inf-a", Healthy: true},
	}
	r, ok := pickRoute(routes, "")
	if !ok || r.Deployment != "MD-00007" {
		t.Fatalf("want the first healthy route, got %q ok=%v", r.Deployment, ok)
	}
	r, ok = pickRoute(routes, "https://box/e/md-00008")
	if !ok || r.Deployment != "MD-00008" {
		t.Fatalf("want the sticky deployment, got %q ok=%v", r.Deployment, ok)
	}
}

// Two placements of one model is the case this whole mechanism exists for: before in-flight
// counts existed every route carried the same zero, so healthy[0] took every request and the
// second engine only ever saw traffic through an explicit session.
func TestPickRouteTakesTheIdleEngine(t *testing.T) {
	routes := []Route{
		{EngineURL: "https://box/e/md-00007", Deployment: "MD-00007", Healthy: true, InFlight: 3},
		{EngineURL: "https://box/e/md-00008", Deployment: "MD-00008", Healthy: true},
	}
	if r, ok := pickRoute(routes, ""); !ok || r.Deployment != "MD-00008" {
		t.Fatalf("want the idle MD-00008, got %q ok=%v", r.Deployment, ok)
	}
}

// Two idle engines tie, and the tie is broken by the caller claiming what it was handed rather
// than by picking at random — so a cold pair alternates on its own.
func TestPickRouteAlternatesFromCold(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true},
		{EngineURL: "b", Healthy: true},
	}
	first, ok := pickRoute(routes, "")
	if !ok || first.EngineURL != "a" {
		t.Fatalf("want a on a tie, got %q ok=%v", first.EngineURL, ok)
	}
	routes[0].InFlight = 1 // what claim() records against the engine just handed out
	if second, ok := pickRoute(routes, ""); !ok || second.EngineURL != "b" {
		t.Fatalf("want b once a is claimed, got %q ok=%v", second.EngineURL, ok)
	}
}
