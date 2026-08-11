package domain

import "testing"

// A model is just a name in the route table, so nothing but its modality distinguishes an ASR
// container from a chat engine. These are the rules that keep a transcription off a chat model.

func TestServes(t *testing.T) {
	for _, c := range []struct {
		modality string
		path     string
		want     bool
		why      string
	}{
		{"text", "/v1/chat/completions", true, "the ordinary case"},
		{"text", "/v1/completions", true, "legacy completions is still text"},
		{"multimodal", "/v1/chat/completions", true, "images ride inside the chat body"},
		{"embedding", "/v1/embeddings", true, ""},
		{"audio", "/v1/audio/transcriptions", true, ""},
		{"audio", "/v1/audio/translations", true, ""},

		{"text", "/v1/audio/transcriptions", false, "a chat engine would 404 this after a round trip"},
		{"multimodal", "/v1/audio/transcriptions", false, "multimodal is images, not audio uploads"},
		{"audio", "/v1/chat/completions", false, "an ASR box cannot hold a conversation"},
		{"embedding", "/v1/chat/completions", false, ""},
		{"text", "/v1/embeddings", false, ""},

		// Generosity is deliberate: this refuses only what another modality has claimed.
		{"text", "/v1/rerank", true, "engines serve more than the OpenAI core"},
		{"audio", "/tokenize", true, "an unclaimed path is nobody's to refuse"},
		{"embedding", "/v1/score", true, ""},

		// Never take traffic away over a value this build does not recognise.
		{"", "/v1/audio/transcriptions", true, "blank = a control plane predating the field"},
		{"   ", "/v1/audio/transcriptions", true, "blank after trimming is still blank"},
		{"video", "/v1/chat/completions", true, "newer control plane, older binary — must not refuse"},

		{"audio", "/v1/audio/transcriptions/", true, "a trailing slash is the same endpoint"},
	} {
		if got := Serves(c.modality, c.path); got != c.want {
			t.Errorf("Serves(%q, %q) = %v, want %v — %s", c.modality, c.path, got, c.want, c.why)
		}
	}
}
