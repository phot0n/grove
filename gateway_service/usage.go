package main

import "encoding/json"

// Usage is a normalized token count parsed from an engine response, whether the
// response was OpenAI-shaped (prompt/completion/total_tokens) or Anthropic-shaped
// (input/output_tokens + cache_read/creation_input_tokens — what vLLM's native
// /v1/messages returns now that the gateway talks to vLLM directly, no LiteLLM).
//
// Normalized so both shapes mean the same thing: Prompt = FULL input processed
// (including cache), Total = Prompt + Completion, Cached ⊆ Prompt. Budget then =
// Total - Cached on either shape.
type Usage struct {
	Prompt     int
	Completion int
	Total      int
	// Cached = prompt tokens served from the prefix cache (credited).
	//   OpenAI/vLLM shape: prompt_tokens_details.cached_tokens — a SUBSET already
	//     inside prompt_tokens/total_tokens (needs vLLM --enable-prompt-tokens-details).
	//   Anthropic shape: cache_read_input_tokens — SEPARATE from input_tokens (which
	//     excludes it) and there's no total_tokens; we fold it (+ cache_creation)
	//     back into Prompt/Total so the numbers match the OpenAI shape. cache_creation
	//     is a WRITE (billed, not credited) so it lands in Prompt but NOT in Cached.
	Cached int
}

// ParseUsage extracts a token count from a JSON blob. It accepts either a full
// response object containing a "usage" field, or the usage object itself, and
// handles both OpenAI and Anthropic key names. Returns ok=false if no token
// fields are present (e.g. a streaming chunk before the final usage arrives).
func ParseUsage(raw []byte) (Usage, bool) {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
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
