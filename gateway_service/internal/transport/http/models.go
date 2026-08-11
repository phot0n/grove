package http

import (
	"net/http"
	"time"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/transport/respond"
)

// GET /v1/models — the gateway answers directly with the models THIS key may use, instead of
// proxying to a single engine (which only knows its own model).

type modelObject struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	Created int64  `json:"created"`
	OwnedBy string `json:"owned_by"`
}

func (s *Server) handleModels(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	if domain.Bearer(r.Header.Get("Authorization")) == "" {
		// No key at all → the public catalogue, so a prospect can see what is on offer before
		// signing up. A key that is present but wrong still 401s below: answering it with the
		// anonymous list would hide a broken key behind a shorter, plausible one.
		models, err := s.catalog.Public(ctx)
		if err != nil {
			respond.Denial(w, err)
			return
		}
		writeModelList(w, models)
		return
	}

	identity, err := s.admission.Identify(ctx, r.Header.Get("Authorization"))
	if err != nil {
		respond.Denial(w, err)
		return
	}
	// Revoked/inactive cannot list. Over budget still can: this shows what the key is entitled to,
	// and that lives on the user, so only inference is blocked.
	if identity.Key.Status != "active" {
		respond.Error(w, http.StatusUnauthorized, "unknown or revoked api key")
		return
	}
	models, err := s.catalog.ForIdentity(ctx, identity)
	if err != nil {
		respond.Denial(w, err)
		return
	}
	writeModelList(w, models)
}

func writeModelList(w http.ResponseWriter, models []string) {
	created := time.Now().Unix()
	data := make([]modelObject, 0, len(models))
	for _, id := range models {
		data = append(data, modelObject{ID: id, Object: "model", Created: created, OwnedBy: "frappe"})
	}
	respond.JSON(w, map[string]any{"object": "list", "data": data})
}
