// Package respond is the one place that decides what an error looks like on the wire. Handlers and
// middleware both write through it, so a refusal reads the same whichever layer produced it.
package respond

import (
	"encoding/json"
	"errors"
	"net/http"

	"grove-gateway/internal/domain"
)

// Error writes the shape an OpenAI client expects to parse.
func Error(w http.ResponseWriter, status int, message string) {
	Status(w, status, map[string]any{
		"error": map[string]any{"message": message, "type": "grove_gateway"},
	})
}

// Denial answers whatever the admission path refused with. Anything that is not a Denial is a bug
// in this process rather than a decision about the caller, so it is a 500 and says nothing more.
func Denial(w http.ResponseWriter, err error) {
	var denial domain.Denial
	if errors.As(err, &denial) {
		Error(w, denial.Status, denial.Reason)
		return
	}
	Error(w, http.StatusInternalServerError, "gateway error")
}

func JSON(w http.ResponseWriter, v any) { Status(w, http.StatusOK, v) }

func Status(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
