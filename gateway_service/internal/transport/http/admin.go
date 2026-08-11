package http

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"net/http"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository"
	"grove-gateway/internal/transport/respond"
)

// The control plane's push/pull surface, token-gated on X-Grove-Admin-Token. Grove is the source of
// truth; these endpoints project its state into this box's store and nothing more.

type adminKey struct {
	KeyHash string `json:"key_hash"` // sha256(secret) hex — the record id
	Prefix  string `json:"prefix"`
	User    string `json:"user"`   // Grove User doc name — the pointer to the user record
	Status  string `json:"status"` // active | revoked
}

type adminUser struct {
	Name    string `json:"name"`
	Email   string `json:"email"`
	Group   string `json:"group"` // "" = ungrouped
	Allow   string `json:"allow"` // comma list: this user's adds on top of the group
	Deny    string `json:"deny"`  // comma list: removals that beat every grant
	Limited bool   `json:"limited"`
}

type adminGroup struct {
	Name   string `json:"name"`
	Models string `json:"models"` // comma list; "" = grants nothing
}

// PUT /admin/keys — upsert. DELETE /admin/keys — remove them. Revocation deletes the credential
// rather than flagging it, so the DELETE is what takes a revoked key out of service.
func (s *Server) handleAdminKeys(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodDelete {
		s.deleteRecords(w, r, s.provisioning.DeleteKeys)
		return
	}
	var body struct {
		Keys []adminKey `json:"keys"`
	}
	if !decodeBody(w, r, &body) {
		return
	}
	records := make([]repository.KeyUpsert, 0, len(body.Keys))
	for _, k := range body.Keys {
		records = append(records, repository.KeyUpsert{
			MeterID: k.KeyHash, Prefix: k.Prefix, User: k.User, Status: k.Status,
		})
	}
	if err := s.provisioning.UpsertKeys(r.Context(), records); err != nil {
		respond.Error(w, http.StatusServiceUnavailable, "key store error")
		return
	}
	respond.JSON(w, map[string]any{"ok": true, "count": len(body.Keys)})
}

// PUT /admin/users — upsert the access and budget state behind one or more Grove Users. The point
// of the split: a person's keys are credentials, and this pushes one record instead of one per key.
func (s *Server) handleAdminUsers(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodDelete {
		s.deleteRecords(w, r, s.provisioning.DeleteUsers)
		return
	}
	var body struct {
		Users []adminUser `json:"users"`
	}
	if !decodeBody(w, r, &body) {
		return
	}
	records := make([]repository.UserUpsert, 0, len(body.Users))
	for _, u := range body.Users {
		records = append(records, repository.UserUpsert{
			Name: u.Name, Email: u.Email, Group: u.Group,
			Allow: u.Allow, Deny: u.Deny, Limited: u.Limited,
		})
	}
	if err := s.provisioning.UpsertUsers(r.Context(), records); err != nil {
		respond.Error(w, http.StatusServiceUnavailable, "user store error")
		return
	}
	respond.JSON(w, map[string]any{"ok": true, "count": len(body.Users)})
}

// PUT /admin/groups — upsert what each group grants, plus the pooled public catalogue. Upsert-only:
// a group nobody links to is unreachable, not harmful.
func (s *Server) handleAdminGroups(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Groups  []adminGroup `json:"groups"`
		Catalog *string      `json:"catalog"` // nil = a control plane that predates it
	}
	if !decodeBody(w, r, &body) {
		return
	}
	records := make([]repository.GroupUpsert, 0, len(body.Groups))
	for _, g := range body.Groups {
		records = append(records, repository.GroupUpsert{
			Name: g.Name, Models: g.Models,
		})
	}
	if err := s.provisioning.UpsertGroups(r.Context(), records, body.Catalog); err != nil {
		respond.Error(w, http.StatusServiceUnavailable, "group store error")
		return
	}
	respond.JSON(w, map[string]any{"ok": true, "count": len(body.Groups)})
}

// PUT /admin/routes — replace the routing table for each given model. Grove sends the full healthy
// set per model each sync.
func (s *Server) handleAdminRoutes(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Routes map[string][]domain.Route `json:"routes"`
		Prune  bool                      `json:"prune"`
	}
	if !decodeBody(w, r, &body) {
		return
	}
	models, pruned, err := s.provisioning.ReplaceRoutes(r.Context(), body.Routes, body.Prune)
	if err != nil {
		respond.Error(w, http.StatusServiceUnavailable, "route store error")
		return
	}
	respond.JSON(w, map[string]any{"ok": true, "models": models, "pruned": pruned})
}

// GET /admin/usage — pull: atomically read-and-delete every live counter. Mutating, and there is no
// second round trip, so a failed insert control-plane-side drops that cycle's delta rather than
// double-counting it.
func (s *Server) handleAdminUsage(w http.ResponseWriter, r *http.Request) {
	usages, err := s.provisioning.DrainUsage(r.Context())
	if err != nil {
		respond.Error(w, http.StatusInternalServerError, "usage store error")
		return
	}
	respond.JSON(w, map[string]any{"usages": usages})
}

func (s *Server) deleteRecords(w http.ResponseWriter, r *http.Request, remove func(context.Context, []string) (int, error)) {
	var body struct {
		IDs []string `json:"ids"`
	}
	if !decodeBody(w, r, &body) {
		return
	}
	count, err := remove(r.Context(), body.IDs)
	if err != nil {
		respond.Error(w, http.StatusServiceUnavailable, "store error")
		return
	}
	respond.JSON(w, map[string]any{"ok": true, "count": count})
}

// adminAuth gates every control-plane endpoint on the shared token, compared in constant time. The
// process refuses to start without one, so a blank token here cannot mean "admin off".
func adminAuth(token string, next http.HandlerFunc) http.HandlerFunc {
	want := []byte(token)
	return func(w http.ResponseWriter, r *http.Request) {
		got := []byte(r.Header.Get("X-Grove-Admin-Token"))
		if len(want) == 0 || subtle.ConstantTimeCompare(got, want) != 1 {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		next(w, r)
	}
}

func decodeBody(w http.ResponseWriter, r *http.Request, into any) bool {
	if err := json.NewDecoder(r.Body).Decode(into); err != nil {
		http.Error(w, "bad body", http.StatusBadRequest)
		return false
	}
	return true
}
