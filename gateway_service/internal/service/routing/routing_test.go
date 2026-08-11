package routing

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository/memory"
)

// The pick rule itself is domain.PickRoute and is covered there. What is only true at this layer is
// the plumbing around it: which sticky key is read and written, when the synthetic session applies,
// that a slot is always claimed, and that an unreadable counter degrades instead of refusing.

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func engine(url string) domain.Route {
	return domain.Route{EngineURL: url, Healthy: true, Deployment: "MD-" + url}
}

func serviceOver(store *memory.Store, opts Options) *Service {
	return New(store.Repositories(), quiet(), opts)
}

// fixedTTL is the synthetic-session knob pinned for a test. In production it is read per pick, so
// turning it is an edit to the tunables file and a signal.
func fixedTTL(d time.Duration) func() time.Duration { return func() time.Duration { return d } }

func twoEngines() *memory.Store {
	store := memory.New()
	store.Routes["qwen3-4b"] = []domain.Route{engine("https://a"), engine("https://b")}
	return store
}

func pick(t *testing.T, svc *Service, session string) Decision {
	t.Helper()
	decision, err := svc.Pick(context.Background(), Request{
		Model: "qwen3-4b", Session: session, MeterID: "meter", KeyPrefix: "abc123",
	})
	if err != nil {
		t.Fatalf("Pick: %v", err)
	}
	return decision
}

// The default. Two idle engines and a caller that names no session must not land on the same box
// every time — that is the bug the whole least-in-flight path exists to fix.
func TestAKeylessCallerIsBalanced(t *testing.T) {
	store := twoEngines()
	svc := serviceOver(store, Options{GatewayID: "gw"})

	first := pick(t, svc, "")
	second := pick(t, svc, "")
	if first.EngineURL() == second.EngineURL() {
		t.Fatalf("both requests went to %s — the claim did not break the tie", first.EngineURL())
	}
}

// A caller that names a session keeps its engine, so its prefix cache stays warm.
func TestANamedSessionKeepsItsEngine(t *testing.T) {
	svc := serviceOver(twoEngines(), Options{GatewayID: "gw"})

	first := pick(t, svc, "acme-bot")
	for i := 0; i < 5; i++ {
		if got := pick(t, svc, "acme-bot").EngineURL(); got != first.EngineURL() {
			t.Fatalf("request %d moved to %s, want %s", i, got, first.EngineURL())
		}
	}
}

// The rollback lever. With it set, a whole key is pinned to one engine — what a single-placement
// fleet always did — even though the caller named no session of its own.
func TestTheSyntheticSessionPinsAKeylessCaller(t *testing.T) {
	store := twoEngines()
	svc := serviceOver(store, Options{GatewayID: "gw", SyntheticTTL: fixedTTL(StickyTTL)})

	first := pick(t, svc, "")
	for i := 0; i < 5; i++ {
		if got := pick(t, svc, "").EngineURL(); got != first.EngineURL() {
			t.Fatalf("request %d moved to %s — the synthetic session did not hold", i, got)
		}
	}
}

// Every admitted request claims a slot, and that claim is what the next pick balances against.
func TestPickClaimsASlotAndReleaseGivesItBack(t *testing.T) {
	store := twoEngines()
	svc := serviceOver(store, Options{GatewayID: "gw"})

	decision := pick(t, svc, "acme-bot")
	if got := len(store.InFlight[decision.EngineURL()]); got != 1 {
		t.Fatalf("in-flight = %d, want 1", got)
	}
	svc.Release(context.Background(), decision.EngineURL(), decision.RequestID)
	if got := len(store.InFlight[decision.EngineURL()]); got != 0 {
		t.Errorf("in-flight = %d after release, want 0 — the engine stays out of rotation", got)
	}
}

// Stickiness loses to capacity: a warm prefix cache is not worth queueing behind a full engine
// when a replica is idle.
func TestAFullStickyEngineIsAbandoned(t *testing.T) {
	store := memory.New()
	full, idle := engine("https://full"), engine("https://idle")
	full.Capacity = 1
	store.Routes["qwen3-4b"] = []domain.Route{full, idle}
	store.Sticky["acme-bot"] = "https://full"
	store.InFlight["https://full"] = map[string]bool{"someone-else": true}

	svc := serviceOver(store, Options{GatewayID: "gw"})
	if got := pick(t, svc, "acme-bot").EngineURL(); got != "https://idle" {
		t.Errorf("picked %s, want the idle replica", got)
	}
}

// A target that has failed EjectAfter times in a row stops being chosen, from live traffic alone.
func TestAnEjectedTargetIsNotChosen(t *testing.T) {
	store := twoEngines()
	store.Failures["https://a"] = domain.EjectAfter

	svc := serviceOver(store, Options{GatewayID: "gw"})
	for i := 0; i < 5; i++ {
		if got := pick(t, svc, "").EngineURL(); got != "https://b" {
			t.Fatalf("picked the ejected target %s", got)
		}
	}
}

// Ejection is an optimisation on top of a table that is already correct. An unreadable health
// counter must not take a working engine out — that would turn one broken store into an outage.
func TestAnUnreadableHealthCounterStillRoutes(t *testing.T) {
	store := twoEngines()
	store.Fail["health"] = true

	if got := pick(t, serviceOver(store, Options{GatewayID: "gw"}), "").EngineURL(); got == "" {
		t.Error("a health-store failure refused a request the route table could serve")
	}
}

// Same rule for the in-flight counts: falling back to the first healthy route beats 503ing a
// working engine over a counter.
func TestUnreadableInFlightCountsStillRoute(t *testing.T) {
	store := twoEngines()
	store.Fail["inflight"] = true

	if got := pick(t, serviceOver(store, Options{GatewayID: "gw"}), "").EngineURL(); got == "" {
		t.Error("an in-flight-store failure refused a request the route table could serve")
	}
}

func TestPickRefusals(t *testing.T) {
	full := engine("https://full")
	full.Capacity = 1

	for _, c := range []struct {
		name   string
		table  []domain.Route
		busy   map[string]bool
		want   int
		reason string
	}{
		{"a model with no placements", nil, nil, 503, "model unavailable"},
		{"every placement unhealthy",
			[]domain.Route{{EngineURL: "https://a"}}, nil, 503, "no healthy server for model"},
		{"every replica at capacity",
			[]domain.Route{full}, map[string]bool{"other": true}, 429, ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			store := memory.New()
			if c.table != nil {
				store.Routes["qwen3-4b"] = c.table
			}
			if c.busy != nil {
				store.InFlight["https://full"] = c.busy
			}
			_, err := serviceOver(store, Options{GatewayID: "gw"}).Pick(
				context.Background(), Request{Model: "qwen3-4b", MeterID: "meter"})

			var denial domain.Denial
			if !errors.As(err, &denial) {
				t.Fatalf("err = %v, want a Denial", err)
			}
			if denial.Status != c.want {
				t.Errorf("status = %d (%s), want %d", denial.Status, denial.Reason, c.want)
			}
		})
	}
}

// A caller-chosen session can name the tenant. Only its hash crosses to the infra plane, and only
// for a route that actually hands off.
func TestOnlyAnIngressRouteCarriesASessionKey(t *testing.T) {
	store := memory.New()
	ingress := engine("https://ingress")
	ingress.Kind = "ingress"
	store.Routes["qwen3-4b"] = []domain.Route{ingress}

	decision := pick(t, serviceOver(store, Options{GatewayID: "gw"}), "acme-support-bot")
	if decision.SessionKey != domain.SHA256Hex("acme-support-bot") {
		t.Errorf("session key = %q, want the hash", decision.SessionKey)
	}
	if decision.SessionKey == "acme-support-bot" {
		t.Error("the caller's own session string reached the infra plane")
	}

	direct := pick(t, serviceOver(twoEngines(), Options{GatewayID: "gw"}), "acme-support-bot")
	if direct.SessionKey != "" {
		t.Errorf("a direct route carried a session key (%q); vLLM has no use for one", direct.SessionKey)
	}
}

// The ingress tier: same rule, its own sticky key, and no region — every replica is in its own VPC.
func TestPickReplicaPinsOnTheForwardedKey(t *testing.T) {
	store := twoEngines()
	svc := serviceOver(store, Options{GatewayID: "gw"})
	const sessionKey = "opaque-hash"

	first, err := svc.PickReplica(context.Background(), "qwen3-4b", sessionKey, "rid-1")
	if err != nil {
		t.Fatalf("PickReplica: %v", err)
	}
	if store.Sticky[sessionKey] != first.EngineURL {
		t.Errorf("pinned %q, want %q", store.Sticky[sessionKey], first.EngineURL)
	}
	second, err := svc.PickReplica(context.Background(), "qwen3-4b", sessionKey, "rid-2")
	if err != nil || second.EngineURL != first.EngineURL {
		t.Errorf("second pick went to %q, want %q", second.EngineURL, first.EngineURL)
	}
}

// The gateway must not eject a whole network over one model that has nowhere to go inside it, so
// this refusal is named rather than left as a bare 503.
func TestAModelWithNoReplicaIsNamedAsSuch(t *testing.T) {
	_, err := serviceOver(memory.New(), Options{}).PickReplica(context.Background(), "gone", "", "rid")

	var denial domain.Denial
	if !errors.As(err, &denial) || denial.Reason != "no-replica" {
		t.Fatalf("err = %v, want a no-replica Denial", err)
	}
}

// A model whose modality does not cover the requested surface is refused before an engine is
// chosen — so nothing is dialled, no slot is claimed, and the meter stage below never runs.
func TestAWrongSurfaceIsRefusedBeforeAnythingIsClaimed(t *testing.T) {
	store := memory.New()
	store.Routes["nemotron-asr"] = []domain.Route{{
		EngineURL: "https://asr", Healthy: true, Deployment: "pod-1", Modality: "audio",
	}}
	svc := serviceOver(store, Options{GatewayID: "gw-test"})

	_, err := svc.Pick(context.Background(), Request{
		Model: "nemotron-asr", MeterID: "meter", KeyPrefix: "abc123",
		Path: "/v1/chat/completions",
	})

	var denial domain.Denial
	if !errors.As(err, &denial) {
		t.Fatalf("err = %v, want a denial", err)
	}
	if denial.Status != 404 {
		t.Errorf("status = %d, want 404", denial.Status)
	}
	if len(store.InFlight) != 0 {
		t.Errorf("a refused request claimed a slot: %v", store.InFlight)
	}
}

// The same model on the surface it is for still routes.
func TestTheRightSurfaceStillRoutes(t *testing.T) {
	store := memory.New()
	store.Routes["nemotron-asr"] = []domain.Route{{
		EngineURL: "https://asr", Healthy: true, Deployment: "pod-1", Modality: "audio",
	}}
	svc := serviceOver(store, Options{GatewayID: "gw-test"})

	decision, err := svc.Pick(context.Background(), Request{
		Model: "nemotron-asr", MeterID: "meter", KeyPrefix: "abc123",
		Path: "/v1/audio/transcriptions",
	})
	if err != nil {
		t.Fatalf("Pick: %v", err)
	}
	if decision.Route.EngineURL != "https://asr" {
		t.Errorf("engine = %q", decision.Route.EngineURL)
	}
}

// A route pushed before modality existed carries none, and must keep serving what it always did.
func TestARouteWithoutAModalityIsUnrestricted(t *testing.T) {
	svc := serviceOver(twoEngines(), Options{GatewayID: "gw-test"})

	if _, err := svc.Pick(context.Background(), Request{
		Model: "qwen3-4b", MeterID: "meter", KeyPrefix: "abc123",
		Path: "/v1/audio/transcriptions",
	}); err != nil {
		t.Fatalf("a blank modality refused a request: %v", err)
	}
}
