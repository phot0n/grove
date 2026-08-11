package transform

import (
	"encoding/json"
	"testing"
)

func decode(t *testing.T, raw string) Body {
	t.Helper()
	var body Body
	if err := json.Unmarshal([]byte(raw), &body); err != nil {
		t.Fatalf("decode %s: %v", raw, err)
	}
	return body
}

func chain(t *testing.T, names ...string) *Chain {
	t.Helper()
	c, err := NewChain(names)
	if err != nil {
		t.Fatalf("NewChain(%v): %v", names, err)
	}
	return c
}

// A misspelt name must stop the process. `priority` silently not running is the difference between
// a client being unable to elevate itself and being able to, and nothing downstream looks wrong.
func TestAnUnknownTransformIsAStartupError(t *testing.T) {
	if _, err := NewChain([]string{"priorty"}); err == nil {
		t.Fatal("a misspelt transform name was accepted")
	}
}

// Without this a streaming request reports no usage at all and bills as zero.
func TestStreamUsageIsForcedOnAStreamingCompletion(t *testing.T) {
	body := decode(t, `{"model":"m","stream":true}`)
	changed, err := chain(t, "streamusage").Apply(Context{Path: "/v1/chat/completions"}, body)
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	if got := string(body["stream_options"]); got != `{"include_usage":true}` {
		t.Errorf("stream_options = %s", got)
	}
}

// A caller may have set other stream options. Replacing the object wholesale would be a silent
// rewrite of their request.
func TestStreamUsageKeepsTheCallersOtherOptions(t *testing.T) {
	body := decode(t, `{"stream":true,"stream_options":{"continuous_usage_stats":true}}`)
	if _, err := chain(t, "streamusage").Apply(Context{Path: "/v1/chat/completions"}, body); err != nil {
		t.Fatal(err)
	}
	var options map[string]any
	if err := json.Unmarshal(body["stream_options"], &options); err != nil {
		t.Fatal(err)
	}
	if options["continuous_usage_stats"] != true {
		t.Error("the caller's own stream option was dropped")
	}
	if options["include_usage"] != true {
		t.Error("include_usage was not set")
	}
}

func TestStreamUsageLeavesANonStreamingBodyAlone(t *testing.T) {
	for _, raw := range []string{`{"model":"m"}`, `{"model":"m","stream":false}`} {
		body := decode(t, raw)
		changed, err := chain(t, "streamusage").Apply(Context{Path: "/v1/chat/completions"}, body)
		if err != nil || changed {
			t.Errorf("%s: changed=%v err=%v", raw, changed, err)
		}
		if _, present := body["stream_options"]; present {
			t.Errorf("%s: stream_options was invented", raw)
		}
	}
}

// The gate is the point of Endpoints(): a field one vLLM schema accepts, another rejects.
func TestTransformsAreGatedToTheirEndpoints(t *testing.T) {
	for _, path := range []string{"/v1/embeddings", "/v1/messages", "/v1/rerank"} {
		body := decode(t, `{"model":"m","stream":true}`)
		changed, err := chain(t, "streamusage", "priority").Apply(Context{Path: path, Priority: -10}, body)
		if err != nil || changed {
			t.Errorf("%s: changed=%v err=%v", path, changed, err)
		}
		if len(body) != 2 {
			t.Errorf("%s: body was rewritten: %v", path, body)
		}
	}
}

// Stamped unconditionally, including the baseline 0 — this is what stops a client naming its own
// priority and jumping the queue.
func TestPriorityOverwritesWhateverTheClientSent(t *testing.T) {
	body := decode(t, `{"model":"m","priority":-999}`)
	changed, err := chain(t, "priority").Apply(Context{Path: "/v1/completions", Priority: 0}, body)
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	if got := string(body["priority"]); got != "0" {
		t.Errorf("priority = %s, want 0 — the client elevated itself", got)
	}
}

func TestPriorityIsStampedWhenAbsent(t *testing.T) {
	body := decode(t, `{"model":"m"}`)
	if _, err := chain(t, "priority").Apply(Context{Path: "/v1/completions", Priority: -10}, body); err != nil {
		t.Fatal(err)
	}
	if got := string(body["priority"]); got != "-10" {
		t.Errorf("priority = %s, want -10", got)
	}
}

// A body no transform touched must be forwarded byte-for-byte rather than re-encoded for nothing.
func TestNothingToDoReportsNoChange(t *testing.T) {
	body := decode(t, `{"model":"m","priority":-10}`)
	changed, err := chain(t, "streamusage", "priority").Apply(
		Context{Path: "/v1/completions", Priority: -10}, body)
	if err != nil || changed {
		t.Errorf("changed=%v err=%v, want no change", changed, err)
	}
}

// Values a transform did not touch must survive as they arrived. Decoding into `any` would turn a
// large integer into a float and hand the engine a different number.
func TestUntouchedFieldsKeepTheirExactBytes(t *testing.T) {
	const big = `12345678901234567890`
	body := decode(t, `{"model":"m","seed":`+big+`,"stream":true}`)
	if _, err := chain(t, "streamusage", "priority").Apply(
		Context{Path: "/v1/chat/completions"}, body); err != nil {
		t.Fatal(err)
	}
	if got := string(body["seed"]); got != big {
		t.Errorf("seed = %s, want %s", got, big)
	}
}
