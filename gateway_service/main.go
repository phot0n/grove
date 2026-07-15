// Command grove-gateway is the Go "brain" behind the OpenResty data path on a
// Proxy Server (§6). OpenResty calls it twice per request over localhost HTTP:
//
//	POST /decide  (access phase)  → auth + rate-limit + Tier-1 pick + sticky
//	POST /meter   (log phase)     → record usage + release the in-flight slot
//
// All Redis access lives here; nginx never pipes the token stream through Go.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	stickyTTL = 30 * time.Minute
)

type server struct {
	rdb *redis.Client
}

func main() {
	addr := env("GROVE_AGENT_ADDR", "127.0.0.1:9090")
	redisAddr := env("GROVE_REDIS_ADDR", "127.0.0.1:6379")

	s := &server{rdb: redis.NewClient(&redis.Options{Addr: redisAddr})}
	if err := s.rdb.Ping(context.Background()).Err(); err != nil {
		log.Fatalf("redis unreachable at %s: %v", redisAddr, err)
	}

	adminToken := os.Getenv("GROVE_ADMIN_TOKEN")

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	mux.HandleFunc("/decide", s.handleDecide)
	mux.HandleFunc("/meter", s.handleMeter)
	// Control-plane push/pull surface (token-gated; reached via OpenResty /grove-admin/).
	mux.HandleFunc("/admin/keys", adminAuth(adminToken, s.handleAdminKeys))
	mux.HandleFunc("/admin/routes", adminAuth(adminToken, s.handleAdminRoutes))
	mux.HandleFunc("/admin/usage", adminAuth(adminToken, s.handleAdminUsage))
	if adminToken == "" {
		log.Print("WARNING: GROVE_ADMIN_TOKEN empty — /admin endpoints disabled")
	}

	log.Printf("grove-gateway agent listening on %s (redis %s)", addr, redisAddr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ---- /decide -------------------------------------------------------------

type decideReq struct {
	Authorization string `json:"authorization"` // raw header, "Bearer grove_sk_..."
	Model         string `json:"model"`
	Session       string `json:"session"` // best-effort hint from Lua (header / user field)
}

type decideResp struct {
	Allow       bool   `json:"allow"`
	Status      int    `json:"status"`
	Reason      string `json:"reason,omitempty"`
	EngineURL   string `json:"engine_url,omitempty"`
	InternalKey string `json:"internal_key,omitempty"`
	MeterID     string `json:"meter_id,omitempty"` // = sha256(secret); Lua echoes it to /meter
	Prefix      string `json:"prefix,omitempty"`
	Session     string `json:"session,omitempty"`
}

func (s *server) handleDecide(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req decideReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, decideResp{Allow: false, Status: 400, Reason: "bad decide body"})
		return
	}

	secret := bearer(req.Authorization)
	if secret == "" {
		writeJSON(w, decideResp{Allow: false, Status: 401, Reason: "missing api key"})
		return
	}
	meterID := sha256hex(secret)

	rec, ok, err := s.loadKey(ctx, meterID)
	if err != nil {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "key store error"})
		return
	}
	if !ok {
		// v1: unknown key → reject. Later: cache-aside fetch from Grove DB (§6 job 1).
		writeJSON(w, decideResp{Allow: false, Status: 401, Reason: "unknown api key"})
		return
	}

	// Admission gates: key status (revoked / over monthly budget) + allowed models.
	if status, reason := evaluate(rec, req.Model); status != 200 {
		writeJSON(w, decideResp{Allow: false, Status: status, Reason: reason})
		return
	}

	session := req.Session
	if session == "" {
		session = sha256hex(meterID + "|" + req.Model)[:24]
	}

	routes, err := s.loadRoutes(ctx, req.Model)
	if err != nil || len(routes) == 0 {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "model unavailable"})
		return
	}
	stickyURL, _ := s.rdb.Get(ctx, "sticky:"+session).Result()
	route, ok := pickRoute(routes, stickyURL)
	if !ok {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "no healthy server for model"})
		return
	}
	s.rdb.Set(ctx, "sticky:"+session, route.EngineURL, stickyTTL)

	writeJSON(w, decideResp{
		Allow:       true,
		Status:      200,
		EngineURL:   route.EngineURL,
		InternalKey: route.InternalKey,
		MeterID:     meterID,
		Prefix:      rec.KeyPrefix,
		Session:     session,
	})
}

// ---- /meter --------------------------------------------------------------

type meterReq struct {
	MeterID string `json:"meter_id"`
	Prefix  string `json:"prefix"`
	Usage   string `json:"usage"` // raw JSON captured from the response (may be empty)
}

func (s *server) handleMeter(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req meterReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Prefix == "" {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// Month is NOT tracked here — the control plane stamps the month from its
	// own clock when it pulls, then drains this hash. Key is just usage:<prefix>.
	usageKey := "usage:" + req.Prefix
	s.rdb.HIncrBy(ctx, usageKey, "request_count", 1)

	if u, ok := ParseUsage([]byte(req.Usage)); ok {
		s.rdb.HIncrBy(ctx, usageKey, "prompt_tokens", int64(u.Prompt))
		s.rdb.HIncrBy(ctx, usageKey, "completion_tokens", int64(u.Completion))
		s.rdb.HIncrBy(ctx, usageKey, "total_tokens", int64(u.Total))
		// Cached ⊆ Prompt, already inside Total. Tracked separately so the control
		// plane can bill/rate-limit on total_tokens - cached_tokens.
		s.rdb.HIncrBy(ctx, usageKey, "cached_tokens", int64(u.Cached))
	}
	w.WriteHeader(http.StatusNoContent)
}

// ---- Redis helpers -------------------------------------------------------

func (s *server) loadKey(ctx context.Context, meterID string) (KeyRecord, bool, error) {
	h, err := s.rdb.HGetAll(ctx, "key:"+meterID).Result()
	if err != nil {
		return KeyRecord{}, false, err
	}
	if len(h) == 0 {
		return KeyRecord{}, false, nil
	}
	rec := KeyRecord{
		Status:    h["status"],
		User:      h["user"],
		KeyPrefix: h["prefix"],
	}
	if am := strings.TrimSpace(h["allowed_models"]); am != "" {
		rec.AllowedModels = map[string]bool{}
		for _, m := range strings.Split(am, ",") {
			if m = strings.TrimSpace(m); m != "" {
				rec.AllowedModels[m] = true
			}
		}
	}
	return rec, true, nil
}

func (s *server) loadRoutes(ctx context.Context, model string) ([]Route, error) {
	raw, err := s.rdb.Get(ctx, "deploy:"+model).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var routes []Route
	if err := json.Unmarshal([]byte(raw), &routes); err != nil {
		return nil, err
	}
	return routes, nil
}

// ---- small utils ---------------------------------------------------------

func bearer(h string) string {
	h = strings.TrimSpace(h)
	if strings.HasPrefix(strings.ToLower(h), "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return h
}

func sha256hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func atoi(s string) int {
	n, _ := strconv.Atoi(strings.TrimSpace(s))
	return n
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
