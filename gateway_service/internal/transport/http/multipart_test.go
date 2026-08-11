package http

import (
	"bytes"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"grove-gateway/internal/config"
	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository/memory"
)

// The multipart endpoints — /v1/audio/transcriptions and friends — carry their model in a form
// field instead of a JSON key. Every one of them was refused with a 403 for an empty model name
// before the gateway learned to read the form.

// multipartForm builds a body from name/value pairs, in order. A name prefixed with "@" becomes a
// file part, which is what has to be walked past rather than held.
func multipartForm(t *testing.T, fields ...string) (body []byte, contentType string) {
	t.Helper()
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)
	for i := 0; i+1 < len(fields); i += 2 {
		name, value := fields[i], fields[i+1]
		var (
			part io.Writer
			err  error
		)
		if strings.HasPrefix(name, "@") {
			part, err = writer.CreateFormFile(strings.TrimPrefix(name, "@"), "sample.wav")
		} else {
			part, err = writer.CreateFormField(name)
		}
		if err != nil {
			t.Fatalf("multipart field %q: %v", name, err)
		}
		if _, err := io.WriteString(part, value); err != nil {
			t.Fatalf("multipart write %q: %v", name, err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("multipart close: %v", err)
	}
	return buf.Bytes(), writer.FormDataContentType()
}

func (f *fixture) postForm(path string, body []byte, contentType string, headers ...string) *httptest.ResponseRecorder {
	f.t.Helper()
	r := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+secret)
	r.Header.Set("Content-Type", contentType)
	for i := 0; i+1 < len(headers); i += 2 {
		r.Header.Set(headers[i], headers[i+1])
	}
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)
	return w
}

// The model comes out of the form, the request routes, and the engine receives the bytes the client
// sent — same boundary, same ordering, same encoding. Byte-equality is the assertion that matters:
// a re-encoded form would still parse here and still break a real engine.
func TestAMultipartUploadIsRoutedAndForwardedByteForByte(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	body, contentType := multipartForm(t, "model", "qwen3-4b", "@file", "RIFFfake-wav-payload")

	resp := f.postForm("/v1/audio/transcriptions", body, contentType)

	if resp.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
	}
	if !bytes.Equal(f.seen.body, body) {
		t.Errorf("engine body was rewritten\n got %q\nwant %q", f.seen.body, body)
	}
	// The per-model counter is where the parsed name shows up on a direct route, which deletes
	// X-Grove-Model on the way out.
	for field, want := range map[string]int64{"request_count": 1, "m:request_count:qwen3-4b": 1} {
		if got := f.store.Usage["abc123"][field]; got != want {
			t.Errorf("usage[%s] = %d, want %d", field, got, want)
		}
	}
}

// The OpenAI SDKs send the file before the model, so the field is only reachable by walking past a
// file part. This is the case that decides whether the feature works at all.
func TestAMultipartUploadFindsTheModelAfterTheFile(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	body, contentType := multipartForm(t,
		"@file", strings.Repeat("audio-bytes", 512),
		"response_format", "json",
		"model", "qwen3-4b",
	)

	resp := f.postForm("/v1/audio/transcriptions", body, contentType)

	if resp.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
	}
	if !bytes.Equal(f.seen.body, body) {
		t.Errorf("engine body was rewritten (%d bytes sent, %d received)", len(body), len(f.seen.body))
	}
	if got := f.store.Usage["abc123"]["m:request_count:qwen3-4b"]; got != 1 {
		t.Errorf("m:request_count:qwen3-4b = %d, want 1", got)
	}
}

// An ingress reads no body at all, so a multipart request reaches it with the model as a header —
// the one route kind where the parsed name has to survive the hop explicitly.
func TestAMultipartUploadOverAnIngressCarriesItsModelAsAHeader(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	f.store.Routes["qwen3-4b"][0].Kind = "ingress"
	body, contentType := multipartForm(t, "@file", "RIFFfake", "model", "qwen3-4b")

	if resp := f.postForm("/v1/audio/transcriptions", body, contentType); resp.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
	}
	if f.seen.groveModel != "qwen3-4b" {
		t.Errorf("X-Grove-Model = %q, want qwen3-4b", f.seen.groveModel)
	}
	if !bytes.Equal(f.seen.body, body) {
		t.Error("the ingress received a rewritten body")
	}
}

// Past the real spill threshold, not a lowered one: a file big enough that the capture goes to disk
// still reaches the engine byte for byte. The spill mechanics are unit-tested in the middleware
// package; this is the shipped default carrying a realistic upload through the whole chain.
func TestAnUploadPastTheSpillThresholdStillArrivesIntact(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	body, contentType := multipartForm(t,
		"@file", strings.Repeat("audio-sample-bytes", 120_000), // ~2 MiB, model behind it
		"model", "qwen3-4b",
	)

	resp := f.postForm("/v1/audio/transcriptions", body, contentType)

	if resp.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
	}
	if !bytes.Equal(f.seen.body, body) {
		t.Errorf("engine got %d bytes, client sent %d", len(f.seen.body), len(body))
	}
}

// A form with no model part is still unroutable, and refused for the same reason a JSON body with no
// model is: the grant check has nothing to match.
func TestAMultipartUploadWithNoModelIsRefused(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	body, contentType := multipartForm(t, "@file", "RIFFfake")

	resp := f.postForm("/v1/audio/transcriptions", body, contentType)

	if resp.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", resp.Code)
	}
	if f.seen.path != "" {
		t.Errorf("engine was reached at %q, want no upstream call", f.seen.path)
	}
}

// The size cap applies before anything is read, so an upload past the limit is a 413 rather than a
// body streamed to an engine and cut off halfway.
func TestAnOversizedMultipartUploadIsRefused(t *testing.T) {
	store := memory.New()
	store.Keys[domain.SHA256Hex(secret)] = domain.KeyRecord{Status: "active", User: "ritwik", KeyPrefix: "abc123"}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}
	handler := buildHandler(t, store, config.Config{}, 128)

	body, contentType := multipartForm(t, "model", "qwen3-4b", "@file", strings.Repeat("a", 4096))
	r := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions", bytes.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+secret)
	r.Header.Set("Content-Type", contentType)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)

	if w.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status = %d, want 413", w.Code)
	}
}

// An upload sent without a Content-Length — a chunked request — cannot be measured up front, so the
// cap is enforced mid-stream instead. It has to fail, not hang and not panic, which is the only
// thing worth asserting: the size is discovered after the route was already picked.
func TestAnUnmeasurableMultipartUploadStillHitsTheCap(t *testing.T) {
	store := memory.New()
	store.Keys[domain.SHA256Hex(secret)] = domain.KeyRecord{Status: "active", User: "ritwik", KeyPrefix: "abc123"}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}
	engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		_, _ = io.WriteString(w, `{"text":"hello"}`)
	}))
	t.Cleanup(engine.Close)
	store.Routes["qwen3-4b"] = []domain.Route{{EngineURL: engine.URL, Healthy: true, Kind: "direct"}}
	handler := buildHandler(t, store, config.Config{}, 256)

	body, contentType := multipartForm(t, "model", "qwen3-4b", "@file", strings.Repeat("a", 8192))
	r := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions", bytes.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+secret)
	r.Header.Set("Content-Type", contentType)
	r.ContentLength = -1 // what net/http reports for a chunked body

	done := make(chan int, 1)
	go func() {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		done <- w.Code
	}()
	select {
	case code := <-done:
		if code < 400 {
			t.Errorf("status = %d, want a refusal once the cap is crossed", code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("an oversized chunked upload hung instead of being refused")
	}
}

// `user` is the same best-effort session hint the JSON path reads, and the header still beats it.
// Only a value sent before `model` is seen — the parse stops as soon as it can route.
func TestAMultipartFormPinsItsSession(t *testing.T) {
	for _, tc := range []struct {
		name    string
		headers []string
		want    string
	}{
		{name: "from the form", want: "caller-7"},
		{name: "header wins", headers: []string{"X-Grove-Session", "explicit-9"}, want: "explicit-9"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t, jsonEngine(`{"text":"hello"}`))
			body, contentType := multipartForm(t, "user", "caller-7", "model", "qwen3-4b", "@file", "RIFF")

			if resp := f.postForm("/v1/audio/transcriptions", body, contentType, tc.headers...); resp.Code != http.StatusOK {
				t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
			}
			if _, pinned := f.store.Sticky[tc.want]; !pinned {
				t.Errorf("session %q was not pinned, sticky = %v", tc.want, f.store.Sticky)
			}
		})
	}
}

// Everything above drives the handler directly, which skips net/http's own body plumbing. This one
// goes over TCP through a real server with a real client, and streams the form from a pipe so the
// request is genuinely chunked — no Content-Length, the shape an SDK uploading a large file sends.
func TestARealChunkedMultipartUploadStreamsThrough(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	front := httptest.NewServer(f.handler)
	t.Cleanup(front.Close)

	var sent bytes.Buffer
	reader, pipe := io.Pipe()
	writer := multipart.NewWriter(io.MultiWriter(pipe, &sent))
	go func() {
		field, _ := writer.CreateFormField("model")
		_, _ = io.WriteString(field, "qwen3-4b")
		file, _ := writer.CreateFormFile("file", "sample.wav")
		_, _ = io.WriteString(file, strings.Repeat("audio", 4096))
		_ = writer.Close()
		_ = pipe.Close()
	}()

	request, err := http.NewRequest(http.MethodPost, front.URL+"/v1/audio/transcriptions", reader)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	request.Header.Set("Authorization", "Bearer "+secret)
	request.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := front.Client().Do(request)
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("status = %d, body = %s", resp.StatusCode, body)
	}
	// A pipe has no length net/http can announce, so this went out chunked and arrived at the
	// gateway with ContentLength -1 — past the up-front size check, through the streaming path.
	if !bytes.Equal(f.seen.body, sent.Bytes()) {
		t.Errorf("engine got %d bytes, client sent %d", len(f.seen.body), sent.Len())
	}
}

// A form part named `model` cannot be used to make the gateway hold a large value: it takes a
// bounded prefix and drains the rest, and the body still reaches the engine whole.
func TestAnEnormousModelFieldIsBounded(t *testing.T) {
	f := newFixture(t, jsonEngine(`{"text":"hello"}`))
	body, contentType := multipartForm(t, "model", strings.Repeat("x", 64<<10))

	resp := f.postForm("/v1/audio/transcriptions", body, contentType)

	if resp.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403 for an unknown model", resp.Code)
	}
	if got := len(f.seen.body); got != 0 {
		t.Errorf("engine received %d bytes, want no upstream call", got)
	}
}

// The JSON path must not notice any of this: a body that is not multipart still decodes, routes
// and meters.
func TestJSONBodiesAreUnaffectedByTheMultipartBranch(t *testing.T) {
	f := newFixture(t, jsonEngine(`{`+usageObject+`}`))

	resp := f.post("/v1/chat/completions", `{"model":"qwen3-4b","messages":[]}`)

	if resp.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.Code, resp.Body)
	}
	if got := f.store.Usage["abc123"]["m:total_tokens:qwen3-4b"]; got != 120 {
		t.Errorf("m:total_tokens:qwen3-4b = %d, want 120", got)
	}
}
