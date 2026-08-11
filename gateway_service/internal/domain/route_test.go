package domain

import "testing"

func TestPickRouteFewestInFlight(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 9},
		{EngineURL: "b", Healthy: true, InFlight: 2},
		{EngineURL: "c", Healthy: true, InFlight: 5},
	}
	r, status := PickRoute(routes, "", "")
	if status != 200 || r.EngineURL != "b" {
		t.Fatalf("want fewest-in-flight b, got %q status=%d", r.EngineURL, status)
	}
}

func TestPickRouteStickyHonored(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 9},
		{EngineURL: "b", Healthy: true, InFlight: 2},
	}
	// Sticky to the busier "a" — stickiness wins over load.
	r, status := PickRoute(routes, "a", "")
	if status != 200 || r.EngineURL != "a" {
		t.Fatalf("want sticky a, got %q status=%d", r.EngineURL, status)
	}
}

func TestPickRouteStickyUnhealthyFailsOver(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: false, InFlight: 1}, // pinned server gone
		{EngineURL: "b", Healthy: true, InFlight: 5},
	}
	// Sticky target unhealthy → re-pick among remaining healthy (same model).
	r, status := PickRoute(routes, "a", "")
	if status != 200 || r.EngineURL != "b" {
		t.Fatalf("want failover to b, got %q status=%d", r.EngineURL, status)
	}
}

func TestPickRouteNoneHealthy(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: false},
		{EngineURL: "b", Healthy: false},
	}
	if _, status := PickRoute(routes, "", ""); status != 503 {
		t.Fatalf("status = %d, want 503 when no route is healthy", status)
	}
}

func TestPickRouteSkipsEmptyEngineURL(t *testing.T) {
	routes := []Route{
		{EngineURL: "", Healthy: true, InFlight: 1}, // pushed without a derived engine_url
		{EngineURL: "b", Healthy: true, InFlight: 9},
	}
	if r, status := PickRoute(routes, "", ""); status != 200 || r.EngineURL != "b" {
		t.Fatalf("want b despite higher load, got %q status=%d", r.EngineURL, status)
	}
	if _, status := PickRoute(routes[:1], "", ""); status != 503 {
		t.Fatalf("status = %d, want 503 (not 500) when the only route has no engine URL", status)
	}
}

func TestPickRouteEmpty(t *testing.T) {
	if _, status := PickRoute(nil, "x", ""); status != 503 {
		t.Fatalf("status = %d, want 503 for empty routes", status)
	}
}

func TestPickRouteCarriesTheDeployment(t *testing.T) {
	// Two deployments of one model on one box: identical Server, so only Deployment tells them
	// apart — which is why the request id and the access line are built from it.
	routes := []Route{
		{EngineURL: "https://box/e/md-00007", Deployment: "MD-00007", Server: "inf-a", Healthy: true},
		{EngineURL: "https://box/e/md-00008", Deployment: "MD-00008", Server: "inf-a", Healthy: true},
	}
	r, status := PickRoute(routes, "", "")
	if status != 200 || r.Deployment != "MD-00007" {
		t.Fatalf("want the first healthy route, got %q status=%d", r.Deployment, status)
	}
	r, status = PickRoute(routes, "https://box/e/md-00008", "")
	if status != 200 || r.Deployment != "MD-00008" {
		t.Fatalf("want the sticky deployment, got %q status=%d", r.Deployment, status)
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
	if r, status := PickRoute(routes, "", ""); status != 200 || r.Deployment != "MD-00008" {
		t.Fatalf("want the idle MD-00008, got %q status=%d", r.Deployment, status)
	}
}

// Two idle engines tie, and the tie is broken by the caller claiming what it was handed rather
// than by picking at random — so a cold pair alternates on its own.
func TestPickRouteAlternatesFromCold(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true},
		{EngineURL: "b", Healthy: true},
	}
	first, status := PickRoute(routes, "", "")
	if status != 200 || first.EngineURL != "a" {
		t.Fatalf("want a on a tie, got %q status=%d", first.EngineURL, status)
	}
	routes[0].InFlight = 1 // what claim() records against the engine just handed out
	if second, status := PickRoute(routes, "", ""); status != 200 || second.EngineURL != "b" {
		t.Fatalf("want b once a is claimed, got %q status=%d", second.EngineURL, status)
	}
}

// --max-num-seqs is what the engine runs at once. Past it vLLM queues internally, where the
// gateway can neither see the wait nor spend it on an idle replica — so admission stops at the
// same number.
func TestPickRouteSpillsToAReplicaAtCapacity(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 8, Capacity: 8},
		{EngineURL: "b", Healthy: true, InFlight: 7, Capacity: 8},
	}
	if r, status := PickRoute(routes, "", ""); status != 200 || r.EngineURL != "b" {
		t.Fatalf("want the replica with room, got %q status=%d", r.EngineURL, status)
	}
}

func TestPickRouteRefusesWhenEveryReplicaIsFull(t *testing.T) {
	// 429, not 503: the model is up and serving, the caller just has to come back.
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 8, Capacity: 8},
		{EngineURL: "b", Healthy: true, InFlight: 9, Capacity: 8},
	}
	if _, status := PickRoute(routes, "", ""); status != 429 {
		t.Fatalf("status = %d, want 429", status)
	}
}

func TestAnUnhealthyReplicaIsStillDownNotBusy(t *testing.T) {
	// Full and dead must not read the same: 429 tells a client to retry, 503 says the model has
	// nowhere to go at all.
	routes := []Route{{EngineURL: "a", Healthy: false, InFlight: 8, Capacity: 8}}
	if _, status := PickRoute(routes, "", ""); status != 503 {
		t.Fatalf("status = %d, want 503", status)
	}
}

func TestCapacityBeatsStickiness(t *testing.T) {
	// A warm prefix cache is not worth queueing behind a full engine while a replica sits idle.
	routes := []Route{
		{EngineURL: "a", Healthy: true, InFlight: 8, Capacity: 8},
		{EngineURL: "b", Healthy: true, InFlight: 0, Capacity: 8},
	}
	if r, status := PickRoute(routes, "a", ""); status != 200 || r.EngineURL != "b" {
		t.Fatalf("want the idle b, got %q status=%d", r.EngineURL, status)
	}
}

func TestNoCapacitySetMeansNoCap(t *testing.T) {
	// Blank --max-num-seqs leaves vLLM on its own default, which the control plane does not know
	// — so there is no number to hold the engine to and it must not be treated as zero.
	routes := []Route{{EngineURL: "a", Healthy: true, InFlight: 500}}
	if r, status := PickRoute(routes, "", ""); status != 200 || r.EngineURL != "a" {
		t.Fatalf("want a admitted uncapped, got %q status=%d", r.EngineURL, status)
	}
}

func TestAnUncappedReplicaAbsorbsWhatACappedOneCannot(t *testing.T) {
	routes := []Route{
		{EngineURL: "capped", Healthy: true, InFlight: 8, Capacity: 8},
		{EngineURL: "uncapped", Healthy: true, InFlight: 40},
	}
	if r, status := PickRoute(routes, "", ""); status != 200 || r.EngineURL != "uncapped" {
		t.Fatalf("want the uncapped engine, got %q status=%d", r.EngineURL, status)
	}
}

func TestARouteInOurOwnRegionWins(t *testing.T) {
	// Least-in-flight alone would send this to the idle box on another continent. The
	// cross-region hop costs far more than the queue depth it saves.
	routes := []Route{
		{EngineURL: "far", Healthy: true, Region: "us-east-1", InFlight: 0},
		{EngineURL: "near", Healthy: true, Region: "ap-south-1", InFlight: 5},
	}
	if r, status := PickRoute(routes, "", "ap-south-1"); status != 200 || r.EngineURL != "near" {
		t.Errorf("got %q/%d, want near/200", r.EngineURL, status)
	}
}

func TestAFarRouteBeatsA503(t *testing.T) {
	routes := []Route{{EngineURL: "far", Healthy: true, Region: "us-east-1"}}
	if r, status := PickRoute(routes, "", "ap-south-1"); status != 200 || r.EngineURL != "far" {
		t.Errorf("got %q/%d, want far/200", r.EngineURL, status)
	}
}

func TestAGatewayWithNoRegionTreatsEveryRouteAlike(t *testing.T) {
	routes := []Route{
		{EngineURL: "a", Healthy: true, Region: "us-east-1", InFlight: 3},
		{EngineURL: "b", Healthy: true, Region: "ap-south-1", InFlight: 0},
	}
	if r, status := PickRoute(routes, "", ""); status != 200 || r.EngineURL != "b" {
		t.Errorf("got %q/%d, want the least loaded and 200", r.EngineURL, status)
	}
}

func TestARouteWithNoRegionIsTreatedAsFarAway(t *testing.T) {
	// Every route carried a blank Region before tiering existed, and the safe reading of
	// "unknown" is "not local".
	routes := []Route{
		{EngineURL: "unknown", Healthy: true, InFlight: 0},
		{EngineURL: "near", Healthy: true, Region: "ap-south-1", InFlight: 9},
	}
	if r, status := PickRoute(routes, "", "ap-south-1"); status != 200 || r.EngineURL != "near" {
		t.Errorf("got %q/%d, want near/200", r.EngineURL, status)
	}
}

func TestCapacityStillAppliesInsideTheWinningTier(t *testing.T) {
	// Tiering narrows the field; it does not excuse a full engine. With every local replica at
	// its cap the answer is 429, not a quiet spill to another region.
	routes := []Route{
		{EngineURL: "near", Healthy: true, Region: "ap-south-1", InFlight: 4, Capacity: 4},
		{EngineURL: "far", Healthy: true, Region: "us-east-1"},
	}
	if _, status := PickRoute(routes, "", "ap-south-1"); status != 429 {
		t.Errorf("status %d, want 429", status)
	}
}

func TestAnIngressRouteIsToldApartFromAnEngine(t *testing.T) {
	if !(Route{Kind: "ingress"}).IsIngress() {
		t.Error("an ingress row was not recognised as one")
	}
	// Empty is what every route pushed before the split carries, and those are all engines.
	for _, kind := range []string{"", "direct"} {
		if (Route{Kind: kind}).IsIngress() {
			t.Errorf("kind %q was treated as an ingress", kind)
		}
	}
}

func TestTheIngressNeverSeesTheCallersSession(t *testing.T) {
	// A caller-chosen session can name the tenant ("acme-support-bot"). The ingress keys its
	// stickiness on the hash, so the string itself never leaves the tenant plane.
	const session = "acme-support-bot"
	if key := SHA256Hex(session); key == session || len(key) != 64 {
		t.Errorf("session key %q is not an opaque hash of %q", key, session)
	}
}
