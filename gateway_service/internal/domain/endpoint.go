package domain

import "strings"

// Which model answers on which OpenAI surface. An ASR model and a chat model are both just a name
// in the route table, so without this a transcription request routes happily to a chat engine, gets
// a 404 from it, and the caller is billed a request for the round trip.

// endpointModalities names, per path, the modalities that answer on it. A path no entry claims is
// forwarded whatever the model is: engines serve more than the OpenAI core — /tokenize, /v1/rerank,
// /v1/score — and refusing those here would take away endpoints that work today.
var endpointModalities = map[string]map[string]bool{
	"/v1/chat/completions":     {"text": true, "multimodal": true},
	"/v1/completions":          {"text": true, "multimodal": true},
	"/v1/embeddings":           {"embedding": true},
	"/v1/audio/transcriptions": {"audio": true},
	"/v1/audio/translations":   {"audio": true},
}

// knownModalities is what this build understands. A value outside it comes from a control plane
// newer than this binary, and is treated as unrestricted — a fleet mid-upgrade must not start
// refusing traffic because one side learned a word first.
var knownModalities = map[string]bool{
	"text": true, "multimodal": true, "embedding": true, "audio": true,
}

// Serves reports whether a model of this modality answers on this path. Pure, and deliberately
// generous: it refuses only when the path is claimed by a modality this model does not have.
func Serves(modality, path string) bool {
	modality = strings.TrimSpace(modality)
	if modality == "" || !knownModalities[modality] {
		return true
	}
	allowed, claimed := endpointModalities[strings.TrimRight(path, "/")]
	if !claimed {
		return true
	}
	return allowed[modality]
}
