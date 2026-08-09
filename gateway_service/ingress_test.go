package main

import (
	"fmt"
	"testing"
	"time"
)

func replica(url string, inFlight, capacity int) Route {
	return Route{EngineURL: url, Healthy: true, InFlight: inFlight, Capacity: capacity}
}

func idle(urls ...string) []Route {
	out := make([]Route, 0, len(urls))
	for _, u := range urls {
		out = append(out, replica(u, 0, 0))
	}
	return out
}

// Affinity is stateless: nothing is stored, so the same key against the same table must answer the
// same replica every time. If the choice depended on anything but the key and the engine URLs, an
// agent restart would shred every client's prefix cache.
func TestTheSameKeyLandsOnTheSameReplica(t *testing.T) {
	routes := idle("http://a/e/m", "http://b/e/m", "http://c/e/m")
	first, _ := pickReplica(routes, "sess-1")
	for i := 0; i < 50; i++ {
		got, status := pickReplica(routes, "sess-1")
		if status != 200 || got.EngineURL != first.EngineURL {
			t.Fatalf("call %d: got %q/%d, want %q/200", i, got.EngineURL, status, first.EngineURL)
		}
	}
}

// Table order is not part of the decision. The control plane rebuilds this table on every sync and
// has no reason to return the replicas in a stable order.
func TestTheOrderOfTheTableDoesNotChangeTheAnswer(t *testing.T) {
	forward := idle("http://a/e/m", "http://b/e/m", "http://c/e/m")
	reversed := idle("http://c/e/m", "http://b/e/m", "http://a/e/m")
	for _, key := range []string{"sess-1", "sess-2", "sess-3", "sess-4"} {
		a, _ := pickReplica(forward, key)
		b, _ := pickReplica(reversed, key)
		if a.EngineURL != b.EngineURL {
			t.Errorf("key %q: %q from one order, %q from the other", key, a.EngineURL, b.EngineURL)
		}
	}
}

// Minimal disruption: losing a replica must move only the keys that were on it. A ring rebuilt on
// every change would reshuffle the whole fleet's affinity on one pod restart.
func TestLosingAReplicaMovesOnlyItsOwnKeys(t *testing.T) {
	before := idle("http://a/e/m", "http://b/e/m", "http://c/e/m")
	after := idle("http://a/e/m", "http://b/e/m")

	moved, wereOnC := 0, 0
	for i := 0; i < 300; i++ {
		key := fmt.Sprintf("sess-%d", i)
		was, _ := pickReplica(before, key)
		now, _ := pickReplica(after, key)
		if was.EngineURL == "http://c/e/m" {
			wereOnC++
			continue // these had to move somewhere
		}
		if was.EngineURL != now.EngineURL {
			moved++
		}
	}
	if wereOnC == 0 {
		t.Fatal("no key hashed to the removed replica — the test proves nothing")
	}
	if moved != 0 {
		t.Errorf("%d keys moved that were not on the removed replica", moved)
	}
}

// Keys spread. A hash that bunched them would be stable and useless.
func TestKeysSpreadAcrossReplicas(t *testing.T) {
	routes := idle("http://a/e/m", "http://b/e/m", "http://c/e/m", "http://d/e/m")
	counts := map[string]int{}
	for i := 0; i < 400; i++ {
		got, _ := pickReplica(routes, fmt.Sprintf("sess-%d", i))
		counts[got.EngineURL]++
	}
	if len(counts) != 4 {
		t.Fatalf("only %d of 4 replicas were ever chosen: %v", len(counts), counts)
	}
	for url, n := range counts {
		if n < 50 { // 100 is even; well under that is a badly skewed hash
			t.Errorf("%s took only %d of 400 keys: %v", url, n, counts)
		}
	}
}

// Bounded load. Plain consistent hashing pins a whale onto one replica while its neighbours idle;
// past the bound the key spills to the next in the ring instead.
func TestAHotReplicaSpillsToTheNext(t *testing.T) {
	routes := idle("http://a/e/m", "http://b/e/m", "http://c/e/m")
	target, _ := pickReplica(routes, "whale")

	loaded := make([]Route, len(routes))
	copy(loaded, routes)
	for i := range loaded {
		if loaded[i].EngineURL == target.EngineURL {
			loaded[i].InFlight = 100
		}
	}
	got, status := pickReplica(loaded, "whale")
	if status != 200 {
		t.Fatalf("status %d, want 200", status)
	}
	if got.EngineURL == target.EngineURL {
		t.Errorf("stayed on the hot replica %q instead of spilling", got.EngineURL)
	}
}

func TestAnIdleFleetDoesNotSpill(t *testing.T) {
	// The bound is computed off the mean, which is 0 on an idle fleet. Without the floor every
	// first request would "exceed" it and fall through to least-in-flight, and affinity would
	// only start working once the fleet was busy.
	routes := idle("http://a/e/m", "http://b/e/m", "http://c/e/m")
	got, _ := pickReplica(routes, "sess-1")
	loaded := make([]Route, len(routes))
	copy(loaded, routes)
	for i := range loaded {
		if loaded[i].EngineURL == got.EngineURL {
			loaded[i].InFlight = 1
		}
	}
	again, _ := pickReplica(loaded, "sess-1")
	if again.EngineURL != got.EngineURL {
		t.Errorf("one in-flight request moved the key from %q to %q", got.EngineURL, again.EngineURL)
	}
}

func TestEvenLoadKeepsTheKeyRatherThanRefusingIt(t *testing.T) {
	// Every replica above the bound means the load is even and the bound is simply tight. There
	// is nothing wrong with any of them, so the request is served rather than 429'd.
	routes := []Route{replica("http://a/e/m", 40, 0), replica("http://b/e/m", 41, 0)}
	got, status := pickReplica(routes, "sess-1")
	if status != 200 || got.EngineURL == "" {
		t.Fatalf("got %q/%d, want a replica and 200", got.EngineURL, status)
	}
}

func TestWithNoSessionKeyItBalances(t *testing.T) {
	routes := []Route{replica("http://a/e/m", 5, 0), replica("http://b/e/m", 1, 0)}
	got, status := pickReplica(routes, "")
	if status != 200 || got.EngineURL != "http://b/e/m" {
		t.Errorf("got %q/%d, want the least loaded and 200", got.EngineURL, status)
	}
}

func TestAModelWithNoReplicaHereIs503(t *testing.T) {
	// Distinct from 429 on purpose: the gateway ejects a network on a 502/504, and must NOT eject
	// one on this — the ingress is perfectly healthy, this one model just has nowhere to go in it.
	unhealthy := []Route{{EngineURL: "http://a/e/m", Healthy: false}}
	if _, status := pickReplica(unhealthy, "sess-1"); status != 503 {
		t.Errorf("status %d, want 503", status)
	}
	if _, status := pickReplica(nil, "sess-1"); status != 503 {
		t.Errorf("empty table: status %d, want 503", status)
	}
}

func TestAnEngineWithNoURLIsNotARoute(t *testing.T) {
	// Lua would hand nginx a bare path as proxy_pass and the caller would see a 500 rather than
	// the 503 that a model with nowhere to go actually means.
	if _, status := pickReplica([]Route{{Healthy: true}}, "sess-1"); status != 503 {
		t.Errorf("status %d, want 503", status)
	}
}

func TestEveryReplicaFullIs429(t *testing.T) {
	full := []Route{replica("http://a/e/m", 4, 4), replica("http://b/e/m", 4, 4)}
	if _, status := pickReplica(full, "sess-1"); status != 429 {
		t.Errorf("status %d, want 429", status)
	}
}

func TestCapacityBeatsAffinity(t *testing.T) {
	// A warm prefix cache is not worth queueing behind a full engine when a replica is idle.
	routes := idle("http://a/e/m", "http://b/e/m")
	target, _ := pickReplica(routes, "sess-1")

	full := make([]Route, len(routes))
	copy(full, routes)
	for i := range full {
		if full[i].EngineURL == target.EngineURL {
			full[i].InFlight, full[i].Capacity = 4, 4
		}
	}
	got, status := pickReplica(full, "sess-1")
	if status != 200 || got.EngineURL == target.EngineURL {
		t.Errorf("got %q/%d, want the other replica and 200", got.EngineURL, status)
	}
}

func TestOnlyAGatewayMayPick(t *testing.T) {
	s := &server{ingressToken: "correct-horse"}
	if !s.isGateway("correct-horse") {
		t.Error("the right token was refused")
	}
	for _, wrong := range []string{"", "correct-hors", "correct-horsee", "CORRECT-HORSE"} {
		if s.isGateway(wrong) {
			t.Errorf("token %q was accepted", wrong)
		}
	}
}

func TestAnIngressWithNoTokenRefusesEveryone(t *testing.T) {
	// A misrendered env file leaves this blank. Comparing blank to blank would open every engine
	// in the VPC to anyone who can reach 443, and the firewall in front is not something this
	// process can verify.
	s := &server{ingressToken: ""}
	for _, token := range []string{"", "anything"} {
		if s.isGateway(token) {
			t.Errorf("token %q was accepted by an ingress with no token set", token)
		}
	}
}

func TestABoxIsOnePlaneOrTheOther(t *testing.T) {
	if _, err := resolveMode("gw-1", "ing-1"); err == nil {
		t.Error("a box given both ids was accepted")
	}
	if id, err := resolveMode("", "ing-1"); err != nil || id != "ing-1" {
		t.Errorf("got %q/%v, want ing-1/nil", id, err)
	}
	for _, gateway := range []string{"gw-1", "", "  "} {
		if id, err := resolveMode(gateway, ""); err != nil || id != "" {
			t.Errorf("gateway %q: got %q/%v, want the gateway plane", gateway, id, err)
		}
	}
}

// The session key is what the ingress hashes, so its two properties are what affinity rests on:
// stable inside a window, and not rotating in lockstep across keys.
func TestASessionKeyIsStableInsideItsWindow(t *testing.T) {
	base := time.Unix(1_800_000_000, 0)
	first := sessionKey("meter-1", "qwen3-35b", base)
	for _, after := range []time.Duration{time.Second, time.Minute, 5 * time.Minute} {
		if got := sessionKey("meter-1", "qwen3-35b", base.Add(after)); got != first {
			t.Errorf("+%s rotated the key inside its window", after)
		}
	}
}

func TestASessionKeyEventuallyRotates(t *testing.T) {
	base := time.Unix(1_800_000_000, 0)
	if sessionKey("meter-1", "qwen3-35b", base) == sessionKey("meter-1", "qwen3-35b", base.Add(2*sessionWindow)) {
		t.Error("the key never rotates, so affinity is permanent")
	}
}

func TestTwoModelsUnderOneKeyBalanceIndependently(t *testing.T) {
	base := time.Unix(1_800_000_000, 0)
	if sessionKey("meter-1", "qwen3-35b", base) == sessionKey("meter-1", "llama-70b", base) {
		t.Error("both models share a session key, so they would pin to the same replica")
	}
}

func TestRotationsAreJitteredAcrossKeys(t *testing.T) {
	// A fixed window rotates every session in the fleet at the same instant — one synchronised
	// cache-miss stampede across every replica. Each key's offset must move its boundary.
	base := time.Unix(1_800_000_000, 0)
	rotated := map[int]int{}
	for i := 0; i < 200; i++ {
		meter := fmt.Sprintf("meter-%d", i)
		before := sessionKey(meter, "m", base)
		for step := 1; step <= 30; step++ {
			if sessionKey(meter, "m", base.Add(time.Duration(step)*time.Minute)) != before {
				rotated[step]++
				break
			}
		}
	}
	if len(rotated) < 10 {
		t.Errorf("keys rotate at only %d distinct minutes — not jittered: %v", len(rotated), rotated)
	}
}

func TestAnIngressRouteIsToldApartFromAnEngine(t *testing.T) {
	if !(Route{Kind: "ingress"}).isIngress() {
		t.Error("an ingress row was not recognised as one")
	}
	// Empty is what every route pushed before the split carries, and those are all engines.
	for _, kind := range []string{"", "direct"} {
		if (Route{Kind: kind}).isIngress() {
			t.Errorf("kind %q was treated as an ingress", kind)
		}
	}
}
