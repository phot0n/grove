package main

import "testing"

func TestParseUsageOpenAI(t *testing.T) {
	raw := []byte(`{"id":"x","usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Prompt != 12 || u.Completion != 8 || u.Total != 20 || u.Cached != 0 {
		t.Fatalf("got %+v ok=%v (Cached must default 0 when no details)", u, ok)
	}
}

func TestParseUsageCachedTokens(t *testing.T) {
	// Live vLLM shape (--enable-prompt-tokens-details): cached_tokens is a subset of
	// prompt_tokens and already inside total_tokens.
	raw := []byte(`{"usage":{"prompt_tokens":617,"completion_tokens":5,"total_tokens":622,"prompt_tokens_details":{"cached_tokens":608,"multimodal_tokens":null}}}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Prompt != 617 || u.Total != 622 || u.Cached != 608 {
		t.Fatalf("got %+v ok=%v (want Cached=608 ⊆ Prompt=617, in Total=622)", u, ok)
	}
}

func TestParseUsageCachedNullDetails(t *testing.T) {
	// Flag off (or field absent) → prompt_tokens_details null → Cached stays 0.
	raw := []byte(`{"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"prompt_tokens_details":null}}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Cached != 0 {
		t.Fatalf("got %+v ok=%v (want Cached=0)", u, ok)
	}
}

func TestParseUsageAnthropic(t *testing.T) {
	// Anthropic non-streaming: input_tokens/output_tokens, no total.
	raw := []byte(`{"type":"message","usage":{"input_tokens":30,"output_tokens":5}}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Prompt != 30 || u.Completion != 5 || u.Total != 35 {
		t.Fatalf("got %+v ok=%v (total should derive as 35)", u, ok)
	}
}

func TestParseUsageAnthropicCache(t *testing.T) {
	// Live vLLM native /v1/messages shape (gateway → vLLM direct, no LiteLLM):
	// input_tokens EXCLUDES cache; cache_read is separate; no total_tokens. We fold
	// cache back in so Prompt=617, Total=622, Cached=608 — same as the OpenAI shape.
	raw := []byte(`{"usage":{"input_tokens":9,"output_tokens":5,"cache_creation_input_tokens":0,"cache_read_input_tokens":608}}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Prompt != 617 || u.Completion != 5 || u.Total != 622 || u.Cached != 608 {
		t.Fatalf("got %+v ok=%v (want Prompt=617 Total=622 Cached=608)", u, ok)
	}
	// billable = Total - Cached must equal the non-cached input + output.
	if u.Total-u.Cached != 14 {
		t.Fatalf("billable %d, want 14", u.Total-u.Cached)
	}
}

func TestParseUsageAnthropicCacheCreation(t *testing.T) {
	// cache_creation is a WRITE: counts in Prompt/Total (billed) but NOT credited.
	raw := []byte(`{"usage":{"input_tokens":10,"output_tokens":2,"cache_creation_input_tokens":100,"cache_read_input_tokens":0}}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Prompt != 110 || u.Total != 112 || u.Cached != 0 {
		t.Fatalf("got %+v ok=%v (want Prompt=110 Total=112 Cached=0)", u, ok)
	}
}

func TestParseUsageBareObject(t *testing.T) {
	// The usage object itself, not wrapped.
	raw := []byte(`{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}`)
	u, ok := ParseUsage(raw)
	if !ok || u.Total != 3 {
		t.Fatalf("got %+v ok=%v", u, ok)
	}
}

func TestParseUsageNoUsage(t *testing.T) {
	// A streaming chunk before the final usage frame.
	raw := []byte(`{"choices":[{"delta":{"content":"hi"}}]}`)
	if _, ok := ParseUsage(raw); ok {
		t.Fatal("expected ok=false when no token fields present")
	}
}

func TestParseUsageGarbage(t *testing.T) {
	if _, ok := ParseUsage([]byte("not json")); ok {
		t.Fatal("expected ok=false on invalid json")
	}
}
