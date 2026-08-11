package domain

import (
	"bytes"
	"encoding/json"
)

// Usage is a token count normalized across the OpenAI and Anthropic response shapes, so both mean
// the same thing: Prompt is the FULL input including cache, Total = Prompt + Completion, and
// Cached ⊆ Prompt. Budget is Total - Cached on either.
type Usage struct {
	Prompt     int
	Completion int
	Total      int
	// Cached = prompt tokens served from the prefix cache, credited. Already inside prompt_tokens
	// on the OpenAI shape; separate from input_tokens on the Anthropic one, so it is folded back in
	// here. cache_creation is a write — billed, so it lands in Prompt but never in Cached.
	Cached int
}

// ParseUsage reads a token count from either a full response or a bare usage object, in either key
// dialect. ok=false when no token fields are present — a streaming chunk before the final frame.
func ParseUsage(raw []byte) (Usage, bool) {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(TrimSSE(raw), &m); err != nil {
		return Usage{}, false
	}
	// Unwrap a nested "usage" object if present.
	if uraw, ok := m["usage"]; ok {
		var um map[string]json.RawMessage
		if json.Unmarshal(uraw, &um) == nil {
			m = um
		}
	}
	geti := func(keys ...string) (int, bool) {
		for _, k := range keys {
			if r, ok := m[k]; ok {
				var n int
				if json.Unmarshal(r, &n) == nil {
					return n, true
				}
			}
		}
		return 0, false
	}
	p, pok := geti("prompt_tokens", "input_tokens")
	c, cok := geti("completion_tokens", "output_tokens")
	t, tok := geti("total_tokens")
	// Anthropic cache buckets (separate from input_tokens; only present on that shape).
	cacheRead, crok := geti("cache_read_input_tokens")
	cacheCreate, _ := geti("cache_creation_input_tokens")
	if !pok && !cok && !tok && !crok {
		return Usage{}, false
	}

	u := Usage{Completion: c}
	if crok {
		// Anthropic shape: input_tokens EXCLUDES cache; fold the cache buckets back
		// in so Prompt/Total are the full processed input (matches OpenAI shape).
		// Credit reads only — cache_creation is a write, billed.
		u.Prompt = p + cacheRead + cacheCreate
		u.Cached = cacheRead
		u.Total = u.Prompt + c
	} else {
		// OpenAI/vLLM shape: prompt_tokens already INCLUDES cached; the cached subset
		// is under prompt_tokens_details.cached_tokens (nested — parse out of band).
		u.Prompt = p
		if draw, ok := m["prompt_tokens_details"]; ok {
			var dm map[string]json.RawMessage
			if json.Unmarshal(draw, &dm) == nil {
				if r, ok := dm["cached_tokens"]; ok {
					var n int
					if json.Unmarshal(r, &n) == nil {
						u.Cached = n
					}
				}
			}
		}
		if tok {
			u.Total = t
		} else {
			u.Total = p + c
		}
	}
	return u, true
}

// TrimSSE strips the "data:" framing off a streaming line. The final frame of an OpenAI stream is
// the only place a streaming request's usage appears, and it arrives wrapped. Handled here rather
// than at each call site so both the streaming and non-streaming paths parse the same way.
func TrimSSE(raw []byte) []byte {
	trimmed := bytes.TrimSpace(raw)
	if rest, ok := bytes.CutPrefix(trimmed, []byte("data:")); ok {
		return bytes.TrimSpace(rest)
	}
	return trimmed
}
