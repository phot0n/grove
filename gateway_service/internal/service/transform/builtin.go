package transform

import "encoding/json"

// The two rules the OpenResty data path always applied. Both are gated to the endpoints whose vLLM
// schema carries the field: elsewhere the body is left untouched rather than risking a field the
// schema rejects.

var completions = []string{"/v1/chat/completions", "/v1/completions"}

func init() {
	Register(streamUsage{})
}

// streamUsage guarantees a usage frame on a streaming response. Without it a streaming request
// reports nothing and bills as zero, which is the whole reason metering can trust the stream.
type streamUsage struct{}

func (streamUsage) Name() string        { return "streamusage" }
func (streamUsage) Endpoints() []string { return completions }

func (streamUsage) Apply(_ Context, body Body) (bool, error) {
	var streaming bool
	raw, present := body["stream"]
	if !present {
		return false, nil
	}
	if err := json.Unmarshal(raw, &streaming); err != nil || !streaming {
		// A non-boolean `stream` is the engine's business to reject, not ours to fail on.
		return false, nil
	}

	options := map[string]json.RawMessage{}
	if existing, ok := body["stream_options"]; ok {
		// Merged, not replaced: a caller may have set other options, and dropping them here would
		// be a silent rewrite of their request.
		if err := json.Unmarshal(existing, &options); err != nil {
			options = map[string]json.RawMessage{}
		}
	}
	if include, ok := options["include_usage"]; ok && string(include) == "true" {
		return false, nil
	}
	options["include_usage"] = json.RawMessage("true")

	encoded, err := json.Marshal(options)
	if err != nil {
		return false, err
	}
	body["stream_options"] = encoded
	return true, nil
}
