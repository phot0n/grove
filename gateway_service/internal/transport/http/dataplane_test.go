package http

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"grove-gateway/internal/config"
	"grove-gateway/internal/domain"
	"grove-gateway/internal/observability"
	"grove-gateway/internal/repository/memory"
	"grove-gateway/internal/service/admission"
	"grove-gateway/internal/service/catalog"
	"grove-gateway/internal/service/metering"
	"grove-gateway/internal/service/provisioning"
	"grove-gateway/internal/service/routing"
	"grove-gateway/internal/service/transform"
	"grove-gateway/internal/transport/http/middleware"
	"grove-gateway/internal/transport/http/proxy"
)

// The whole data path against a fake vLLM: what the engine received, what the client got back, and
// what landed in the store. Every one of these was untestable while nginx owned the bytes.

const (
	secret      = "gr_sk_demo"
	usageObject = `"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,` +
		`"prompt_tokens_details":{"cached_tokens":80}}`
)

// engineRecord is what the fake engine saw, so a test can assert on the request as forwarded.
type engineRecord struct {
	path          string
	authorization string
	requestID     string
	groveModel    string
	groveSession  string
	forwardedFor  string
	body          []byte
}

type fixture struct {
	t       *testing.T
	store   *memory.Store
	handler http.Handler
	engine  *httptest.Server
	seen    *engineRecord
}

func newFixture(t *testing.T, engineHandler http.HandlerFunc) *fixture {
	t.Helper()
	seen := &engineRecord{}
	engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		*seen = engineRecord{
			path:          r.URL.Path,
			authorization: r.Header.Get("Authorization"),
			requestID:     r.Header.Get("X-Request-Id"),
			groveModel:    r.Header.Get("X-Grove-Model"),
			groveSession:  r.Header.Get("X-Grove-Session-Key"),
			forwardedFor:  r.Header.Get("X-Forwarded-For"),
			body:          body,
		}
		engineHandler(w, r)
	}))
	t.Cleanup(engine.Close)

	store := memory.New()
	store.Keys[domain.SHA256Hex(secret)] = domain.KeyRecord{
		Status: "active", User: "ritwik", KeyPrefix: "abc123",
	}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}
	store.Routes["qwen3-4b"] = []domain.Route{{
		EngineURL: engine.URL + "/e/md1", InternalKey: "engine-key",
		Healthy: true, Deployment: "MD-00007", Server: "INF-1", Kind: "direct",
	}}

	return &fixture{t: t, store: store, engine: engine, seen: seen, handler: buildHandler(t, store, config.Config{}, 0)}
}

func buildHandler(t *testing.T, store *memory.Store, cfg config.Config, maxBody int64) http.Handler {
	t.Helper()
	logs := observability.Discard()
	repos := store.Repositories()
	transforms, err := transform.NewChain(transform.Default)
	if err != nil {
		t.Fatalf("transform chain: %v", err)
	}
	cfg.AdminToken = "admin-token"
	server := New(cfg, Services{
		Admission:    admission.New(repos.Keys, repos.Users, repos.Groups),
		Routing:      routing.New(repos, logs.Process, routing.Options{GatewayID: "gw-test"}),
		Metering:     metering.New(repos.Usage, repos.Health, logs.Process),
		Catalog:      catalog.New(repos.Routes, repos.Catalog),
		Provisioning: provisioning.New(repos, logs.Process),
		Transform:    transforms,
		Proxy:        proxy.New(proxy.Options{}, logs.Process),
		Access:       logs.Access,
		MaxBodyBytes: func() int64 { return maxBody },
	}, logs.Process)

	handler, err := server.DataHandler(middleware.GatewayChain)
	if err != nil {
		t.Fatalf("DataHandler: %v", err)
	}
	return handler
}

func (f *fixture) post(path, body string, headers ...string) *httptest.ResponseRecorder {
	f.t.Helper()
	r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+secret)
	r.Header.Set("Content-Type", "application/json")
	for i := 0; i+1 < len(headers); i += 2 {
		r.Header.Set(headers[i], headers[i+1])
	}
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)
	return w
}

func jsonEngine(payload string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, payload)
	}
}

// A non-streaming request: forwarded whole, and its usage recorded against the key.
func TestNonStreamingRequestIsMetered(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"id":"chatcmpl-1",`+usageObject+`}`))

	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b","messages":[]}`)
	if resp.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
	}
	usage := f.store.Usage["abc123"]
	for field, want := range map[string]int64{
		"request_count": 1, "prompt_tokens": 100, "completion_tokens": 20,
		"total_tokens": 120, "cached_tokens": 80,
		"m:total_tokens:qwen3-4b": 120, "m:total_tokens:MD-00007": 120,
	} {
		if usage[field] != want {
			t.Errorf("usage[%s] = %d, want %d", field, usage[field], want)
		}
	}
}

// The engine's path is the route's base plus the path the client asked for, and the client's key
// never reaches it.
func TestTheEngineSeesTheRewrittenRequest(t *testing.T) {
	f := newFixture(t, jsonEngine(`{`+usageObject+`}`))
	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b"}`)

	if f.seen.path != "/e/md1/v1/chat/completions" {
		t.Errorf("engine path = %q, want /e/md1/v1/chat/completions", f.seen.path)
	}
	if f.seen.authorization != "Bearer engine-key" {
		t.Errorf("engine authorization = %q — the client's key must not reach an engine", f.seen.authorization)
	}
	if !strings.HasPrefix(f.seen.requestID, "gr-gw_test-MD_00007-abc123-") {
		t.Errorf("engine X-Request-Id = %q", f.seen.requestID)
	}
	if got := resp.Header().Get("X-Request-Id"); got != f.seen.requestID {
		t.Errorf("client got rid %q, engine got %q — the correlation is broken", got, f.seen.requestID)
	}
	if f.seen.forwardedFor == "" {
		t.Error("X-Forwarded-For was not set")
	}
	// A direct route reaches vLLM, which adopts X-Request-Id and nothing else of ours.
	if f.seen.groveModel != "" || f.seen.groveSession != "" {
		t.Errorf("ingress headers leaked to an engine: model=%q session=%q", f.seen.groveModel, f.seen.groveSession)
	}
}

// The transform, seen from the engine's side: a streaming request is guaranteed a usage frame,
// which is what lets metering trust the stream.
func TestTheBodyReachesTheEngineTransformed(t *testing.T) {
	f := newFixture(t, jsonEngine(`{`+usageObject+`}`))
	f.post("/v1/chat/completions", `{"model":"qwen3-4b","stream":true}`)

	var body map[string]any
	if err := json.Unmarshal(f.seen.body, &body); err != nil {
		t.Fatalf("engine body: %v", err)
	}
	options, _ := body["stream_options"].(map[string]any)
	if options["include_usage"] != true {
		t.Errorf("include_usage not forced; body = %s", f.seen.body)
	}
}

// Nothing rewrites a non-streaming body any more, so it reaches the engine exactly as sent —
// including a `priority` the client chose. Engines run fcfs, which ignores it.
func TestANonStreamingBodyIsForwardedUnchanged(t *testing.T) {
	f := newFixture(t, jsonEngine(`{`+usageObject+`}`))
	sent := `{"model":"qwen3-4b","messages":[],"priority":-999}`
	f.post("/v1/chat/completions", sent)

	if string(f.seen.body) != sent {
		t.Errorf("engine body = %s, want it byte-for-byte as sent", f.seen.body)
	}
}

// An endpoint outside the transforms' gate must be forwarded byte-for-byte: a field one vLLM schema
// accepts, another rejects.
func TestAnUngatedEndpointIsForwardedUntouched(t *testing.T) {
	f := newFixture(t, jsonEngine(`{`+usageObject+`}`))
	const body = `{"model":"qwen3-4b","input":"hello"}`
	f.post("/v1/embeddings", body)

	if string(f.seen.body) != body {
		t.Errorf("engine body = %s, want it untouched (%s)", f.seen.body, body)
	}
}

// The property the usage tee exists to preserve: the stream reaches the client unmodified, in the
// chunks the engine wrote them, and the usage frame is still captured.
func TestAStreamIsRelayedUnmodifiedAndStillMetered(t *testing.T) {
	frames := []string{
		`data: {"choices":[{"delta":{"content":"He"}}]}`,
		`data: {"choices":[{"delta":{"content":"llo"}}]}`,
		`data: {"choices":[],` + usageObject + `}`,
		`data: [DONE]`,
	}
	f := newFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, frame := range frames {
			_, _ = io.WriteString(w, frame+"\n\n")
			w.(http.Flusher).Flush()
		}
	})

	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b","stream":true}`)
	want := strings.Join(frames, "\n\n") + "\n\n"
	if resp.Body.String() != want {
		t.Errorf("stream was altered.\n got: %q\nwant: %q", resp.Body.String(), want)
	}
	if got := f.store.Usage["abc123"]["total_tokens"]; got != 120 {
		t.Errorf("total_tokens = %d, want 120 — the usage frame was not captured", got)
	}
}

// A stream split across arbitrary read boundaries must still yield the usage line — the carry
// across chunks is the whole reason the tee is not a simple per-read scan.
func TestAUsageFrameSplitAcrossWritesIsStillCaptured(t *testing.T) {
	payload := `data: {"choices":[],` + usageObject + "}\n\n"
	f := newFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i < len(payload); i += 7 { // deliberately not a frame boundary
			end := min(i+7, len(payload))
			_, _ = io.WriteString(w, payload[i:end])
			w.(http.Flusher).Flush()
		}
	})
	f.post("/v1/chat/completions", `{"model":"qwen3-4b","stream":true}`)

	if got := f.store.Usage["abc123"]["total_tokens"]; got != 120 {
		t.Errorf("total_tokens = %d, want 120", got)
	}
}

// The case a metering call placed after the proxy would miss, and the one that leaves an engine
// counted as busy forever.
func TestAClientDisconnectStillReleasesTheSlot(t *testing.T) {
	started := make(chan struct{})
	f := newFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"choices\":[]}\n\n")
		w.(http.Flusher).Flush()
		close(started)
		<-r.Context().Done() // hold the stream open until the client goes away
	})

	ctx, cancel := context.WithCancel(context.Background())
	r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"qwen3-4b","stream":true}`)).WithContext(ctx)
	r.Header.Set("Authorization", "Bearer "+secret)

	done := make(chan struct{})
	go func() {
		defer close(done)
		f.handler.ServeHTTP(httptest.NewRecorder(), r)
	}()

	<-started
	cancel()
	<-done

	engineURL := f.store.Routes["qwen3-4b"][0].EngineURL
	if got := len(f.store.InFlight[engineURL]); got != 0 {
		t.Errorf("in-flight = %d after a disconnect — the engine stays out of rotation", got)
	}
	if f.store.Usage["abc123"]["request_count"] != 1 {
		t.Error("an abandoned request was not counted at all")
	}
}

// A dead engine is counted against it, so three in a row take it out of rotation.
func TestADeadUpstreamIsA502AndCountsAgainstTheTarget(t *testing.T) {
	f := newFixture(t, jsonEngine(`{}`))
	f.engine.Close() // the route now points at nothing

	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b"}`)
	if resp.Code != http.StatusBadGateway {
		t.Errorf("status = %d, want 502", resp.Code)
	}
	engineURL := f.store.Routes["qwen3-4b"][0].EngineURL
	if f.store.Failures[engineURL] != 1 {
		t.Errorf("failures = %d, want 1", f.store.Failures[engineURL])
	}
	if len(f.store.InFlight[engineURL]) != 0 {
		t.Error("a failed hop left its in-flight slot claimed")
	}
}

// An ingress hop is a hand-off: it reads no body, so the model goes as a header — and the session
// with it, hashed, because it is the tier that must not learn whose request this is.
func TestAnIngressRouteGetsHeadersAndAHashedSession(t *testing.T) {
	f := newFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-Grove-Engine", "MD-00099")
		_, _ = io.WriteString(w, `{`+usageObject+`}`)
	})
	f.store.Routes["qwen3-4b"][0].Kind = "ingress"

	f.post("/v1/chat/completions", `{"model":"qwen3-4b","user":"acme-support-bot"}`)

	if f.seen.groveModel != "qwen3-4b" {
		t.Errorf("X-Grove-Model = %q", f.seen.groveModel)
	}
	if f.seen.groveSession != domain.SHA256Hex("acme-support-bot") {
		t.Errorf("X-Grove-Session-Key = %q, want the hash", f.seen.groveSession)
	}
	if strings.Contains(f.seen.groveSession, "acme") {
		t.Error("the caller's own session string reached the infra plane")
	}
	// The replica the ingress chose, off its response header — the only way usage reaches a
	// placement this gateway never picked.
	if got := f.store.Usage["abc123"]["m:total_tokens:MD-00099"]; got != 120 {
		t.Errorf("m:total_tokens:MD-00099 = %d, want 120", got)
	}
}

func TestDataPathRefusals(t *testing.T) {
	for _, c := range []struct {
		name, body, key string
		want            int
	}{
		{"a model the key may not use", `{"model":"secret-model"}`, secret, http.StatusForbidden},
		// Granted, but every placement is gone. A different answer from the 403 above on purpose:
		// 503 says the model is down, 403 says it was never yours.
		{"a granted model with no placement", `{"model":"drained"}`, secret, http.StatusServiceUnavailable},
		{"an unknown key", `{"model":"qwen3-4b"}`, "nope", http.StatusUnauthorized},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newFixture(t, jsonEngine(`{}`))
			f.store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b,drained")}
			r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(c.body))
			r.Header.Set("Authorization", "Bearer "+c.key)
			w := httptest.NewRecorder()
			f.handler.ServeHTTP(w, r)

			if w.Code != c.want {
				t.Fatalf("status = %d, want %d (%s)", w.Code, c.want, w.Body)
			}
			// Every refusal is OpenAI-shaped, so a client parses it the same way whichever gate
			// produced it.
			var body struct {
				Error struct{ Message, Type string } `json:"error"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || body.Error.Message == "" {
				t.Errorf("refusal body is not OpenAI-shaped: %s", w.Body)
			}
			if f.seen.path != "" {
				t.Error("a refused request still reached the engine")
			}
		})
	}
}

// A refused request must never claim a slot it will not release.
func TestARefusedRequestClaimsNothing(t *testing.T) {
	f := newFixture(t, jsonEngine(`{}`))
	f.post("/v1/chat/completions", `{"model":"secret-model"}`)

	for engine, ids := range f.store.InFlight {
		if len(ids) != 0 {
			t.Errorf("%s holds %d slots after a refusal", engine, len(ids))
		}
	}
}

// The body cap: beyond it the caller gets a 413 rather than the process growing to hold whatever
// was sent.
func TestAnOversizedBodyIsRefused(t *testing.T) {
	store := memory.New()
	store.Keys[domain.SHA256Hex(secret)] = domain.KeyRecord{Status: "active", User: "ritwik", KeyPrefix: "abc123"}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}
	handler := buildHandler(t, store, config.Config{}, 128)

	r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(fmt.Sprintf(`{"model":"qwen3-4b","x":%q}`, strings.Repeat("a", 4096))))
	r.Header.Set("Authorization", "Bearer "+secret)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)

	if w.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status = %d, want 413", w.Code)
	}
}

// /v1/models is answered here and never forwarded — an engine only knows its own model.
func TestModelsIsAnsweredByTheGateway(t *testing.T) {
	f := newFixture(t, jsonEngine(`{}`))
	r := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	r.Header.Set("Authorization", "Bearer "+secret)
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d", w.Code)
	}
	if f.seen.path != "" {
		t.Error("/v1/models was forwarded to an engine")
	}
	var list struct {
		Data []struct{ ID string } `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	if len(list.Data) != 1 || list.Data[0].ID != "qwen3-4b" {
		t.Errorf("models = %v, want just the entitled one", list.Data)
	}
}

// While draining, a NEW request gets a real message with a retry hint rather than a reset.
func TestDrainingAnswersARestartingMessage(t *testing.T) {
	f := newFixture(t, jsonEngine(`{}`))
	f.handler = drainingHandler(t, f.store)

	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b"}`)
	if resp.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", resp.Code)
	}
	if resp.Header().Get("Retry-After") == "" {
		t.Error("no Retry-After — a client has nothing to back off on")
	}
	if !strings.Contains(resp.Body.String(), "restarting") {
		t.Errorf("body = %s, want a restarting message", resp.Body)
	}
	if f.seen.path != "" {
		t.Error("a request was forwarded while draining")
	}
}

type alwaysDraining struct{}

func (alwaysDraining) Draining() bool { return true }

func drainingHandler(t *testing.T, store *memory.Store) http.Handler {
	t.Helper()
	logs := observability.Discard()
	repos := store.Repositories()
	transforms, _ := transform.NewChain(transform.Default)
	server := New(config.Config{AdminToken: "admin-token"}, Services{
		Admission:    admission.New(repos.Keys, repos.Users, repos.Groups),
		Routing:      routing.New(repos, logs.Process, routing.Options{GatewayID: "gw-test"}),
		Metering:     metering.New(repos.Usage, repos.Health, logs.Process),
		Catalog:      catalog.New(repos.Routes, repos.Catalog),
		Provisioning: provisioning.New(repos, logs.Process),
		Transform:    transforms,
		Proxy:        proxy.New(proxy.Options{}, logs.Process),
		Drain:        alwaysDraining{},
		Access:       logs.Access,
	}, logs.Process)
	handler, err := server.DataHandler(middleware.GatewayChain)
	if err != nil {
		t.Fatalf("DataHandler: %v", err)
	}
	return handler
}

// A chain naming a stage that does not exist must stop the process. A misspelt `quota` that merely
// warned would silently stop enforcing the monthly budget.
func TestAnUnknownMiddlewareRefusesToStart(t *testing.T) {
	store := memory.New()
	logs := observability.Discard()
	repos := store.Repositories()
	server := New(config.Config{AdminToken: "t"}, Services{
		Admission: admission.New(repos.Keys, repos.Users, repos.Groups),
		Routing:   routing.New(repos, logs.Process, routing.Options{}),
		Metering:  metering.New(repos.Usage, repos.Health, logs.Process),
		Proxy:     proxy.New(proxy.Options{}, logs.Process),
	}, logs.Process)

	if _, err := server.DataHandler([]string{"recover", "quotaa"}); err == nil {
		t.Fatal("a misspelt middleware name was accepted")
	}
}

// Timing guard for the streaming property: a client must see the first token long before the
// engine finishes, or the response is being buffered somewhere.
func TestTokensArriveBeforeTheResponseEnds(t *testing.T) {
	firstSeen := make(chan time.Time, 1)
	f := newFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n")
		w.(http.Flusher).Flush()
		firstSeen <- time.Now()
		time.Sleep(150 * time.Millisecond)
		_, _ = io.WriteString(w, "data: {"+usageObject+"}\n\n")
	})

	start := time.Now()
	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b","stream":true}`)
	total := time.Since(start)
	flushed := <-firstSeen

	if total < 150*time.Millisecond {
		t.Fatalf("the engine's own delay did not happen; test is not measuring anything (%v)", total)
	}
	if flushed.Sub(start) > 100*time.Millisecond {
		t.Errorf("first token took %v to leave the engine — the request was held up", flushed.Sub(start))
	}
	if !strings.Contains(resp.Body.String(), "hi") {
		t.Error("the first token never reached the client")
	}
}
