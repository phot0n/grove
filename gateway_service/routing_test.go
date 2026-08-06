package main

import "testing"

func TestPickRouteLeastLoaded(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, Load: 0.9},
		{EngineURL: "b", Healthy: true, Load: 0.2},
		{EngineURL: "c", Healthy: true, Load: 0.5},
	}
	r, ok := pickRoute(routes, "")
	if !ok || r.EngineURL != "b" {
		t.Fatalf("want least-loaded b, got %q ok=%v", r.EngineURL, ok)
	}
}

func TestPickRouteStickyHonored(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, Load: 0.9},
		{EngineURL: "b", Healthy: true, Load: 0.2},
	}
	// Sticky to the more-loaded "a" — stickiness wins over load.
	r, ok := pickRoute(routes, "a")
	if !ok || r.EngineURL != "a" {
		t.Fatalf("want sticky a, got %q ok=%v", r.EngineURL, ok)
	}
}

func TestPickRouteStickyUnhealthyFailsOver(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: false, Load: 0.1}, // pinned server gone
		{EngineURL: "b", Healthy: true, Load: 0.5},
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
		{EngineURL: "", Healthy: true, Load: 0.1}, // pushed without a derived engine_url
		{EngineURL: "b", Healthy: true, Load: 0.9},
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
