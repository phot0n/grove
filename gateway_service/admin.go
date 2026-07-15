package main

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/redis/go-redis/v9"
)

// Admin API — the control-plane (Grove) push/pull surface (§6). Token-gated
// (X-Grove-Admin-Token == GROVE_ADMIN_TOKEN). Exposed off-box via OpenResty
// location /grove-admin/. Grove is the source of truth; these endpoints just
// project its state into this Proxy Server's local Redis.

type adminKey struct {
	KeyHash       string `json:"key_hash"` // sha256(secret) hex — the Redis key id
	Prefix        string `json:"prefix"`
	User          string `json:"user"`
	Status        string `json:"status"`         // active | revoked | rate_limited
	AllowedModels string `json:"allowed_models"` // comma list; "" = all
}

// PUT /admin/keys — upsert one or more keys. Revocation is an upsert with
// status="revoked" (the agent's evaluate() then returns 401).
func (s *server) handleAdminKeys(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Keys []adminKey `json:"keys"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad body", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	for _, k := range body.Keys {
		if k.KeyHash == "" {
			continue
		}
		s.rdb.HSet(ctx, "key:"+k.KeyHash, map[string]any{
			"status":         k.Status,
			"user":           k.User,
			"prefix":         k.Prefix,
			"allowed_models": k.AllowedModels,
		})
	}
	writeJSON(w, map[string]any{"ok": true, "count": len(body.Keys)})
}

// PUT /admin/routes — replace the routing table for each given model
// (deploy:<model>). Grove sends the full healthy set per model each sync.
func (s *server) handleAdminRoutes(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Routes map[string][]Route `json:"routes"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad body", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	for model, routes := range body.Routes {
		if len(routes) == 0 {
			s.rdb.Del(ctx, "deploy:"+model) // model fully drained → 503
			continue
		}
		b, _ := json.Marshal(routes)
		s.rdb.Set(ctx, "deploy:"+model, b, 0)
	}
	writeJSON(w, map[string]any{"ok": true, "models": len(body.Routes)})
}

// drainUsage atomically READS and DELETES a live usage:<prefix> hash — the
// snapshot is returned and the counter removed in one Redis call. Usage pull is
// 1-shot / no retry: once drained the delta lives only in the HTTP response, so
// a control-plane crash before it commits loses that cycle's delta (rare,
// bounded — but never double-count). Atomic (Redis single-threaded): a request
// metered mid-pull lands either fully in the returned snapshot (before the DEL)
// or on a fresh key (after), never split. HGETALL on a missing key is empty and
// DEL is a no-op, so no EXISTS guard is needed.
var drainUsage = redis.NewScript(`
local h = redis.call('HGETALL', KEYS[1])
redis.call('DEL', KEYS[1])
return h
`)

// GET /admin/usage — pull: atomically read-and-delete every live usage:<prefix>
// counter and return the snapshots keyed by bare prefix. Mutating: the counters
// are removed so new requests accrue on a fresh key. The control plane stamps
// the month and inserts these; there is no second round trip (the DEL already
// happened here), so a failed insert simply drops that cycle's delta.
func (s *server) handleAdminUsage(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	out := map[string]map[string]string{}
	var cursor uint64
	for {
		keys, next, err := s.rdb.Scan(ctx, cursor, "usage:*", 200).Result()
		if err != nil {
			http.Error(w, "scan error", http.StatusInternalServerError)
			return
		}
		for _, live := range keys {
			res, err := drainUsage.Run(ctx, s.rdb, []string{live}).Result()
			if err != nil {
				continue
			}
			// SCAN can return a key more than once; the 2nd drain finds it
			// already deleted (empty). Don't clobber the snapshot we already
			// captured for this prefix.
			if m := pairsToMap(res); len(m) > 0 {
				out[strings.TrimPrefix(live, "usage:")] = m
			}
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	writeJSON(w, map[string]any{"usages": out})
}

// pairsToMap turns a HGETALL reply ([field, val, field, val, ...]) into a map.
func pairsToMap(res any) map[string]string {
	m := map[string]string{}
	arr, ok := res.([]any)
	if !ok {
		return m
	}
	for i := 0; i+1 < len(arr); i += 2 {
		k, _ := arr[i].(string)
		v, _ := arr[i+1].(string)
		m[k] = v
	}
	return m
}

// adminAuth wraps the admin handlers with the shared-token check.
func adminAuth(token string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if token == "" || r.Header.Get("X-Grove-Admin-Token") != token {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		next(w, r)
	}
}
