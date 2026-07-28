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
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	stickyTTL = 30 * time.Minute
)

type server struct {
	rdb       *redis.Client
	gatewayID string // this gateway's id — the first part of every request-id
}

func main() {
	addr := env("GROVE_AGENT_ADDR", "127.0.0.1:9090")
	redisAddr := env("GROVE_REDIS_ADDR", "127.0.0.1:6379")

	s := &server{rdb: redis.NewClient(&redis.Options{Addr: redisAddr}), gatewayID: gatewayID()}
	if err := s.rdb.Ping(context.Background()).Err(); err != nil {
		log.Fatalf("redis unreachable at %s: %v", redisAddr, err)
	}

	adminToken := os.Getenv("GROVE_ADMIN_TOKEN")

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	mux.HandleFunc("/decide", s.handleDecide)
	mux.HandleFunc("/meter", s.handleMeter)
	mux.HandleFunc("/models", s.handleModels)
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
	RequestID   string `json:"request_id,omitempty"` // gr-<gateway>-<server>-<keyprefix>-<rand>
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
		RequestID:   s.buildRequestID(route, rec.KeyPrefix),
	})
}

// buildRequestID stamps a traceable id on every admitted request:
// gr-<gateway>-<inference server>-<key prefix>-<random>. The key prefix is the API Key's
// unique doc name (already unique per key); the random tail makes each request unique. Parts
// are sanitized so the only '-' is the separator. Server falls back to a short hash of the
// engine URL for legacy routes pushed without a server id.
func (s *server) buildRequestID(route Route, keyPrefix string) string {
	srv := route.Server
	if srv == "" {
		srv = sha256hex(route.EngineURL)[:8]
	}
	return fmt.Sprintf("gr-%s-%s-%s-%s",
		cleanIDPart(s.gatewayID), cleanIDPart(srv), cleanIDPart(keyPrefix), randHex(6))
}

// ---- /meter --------------------------------------------------------------

type meterReq struct {
	MeterID string `json:"meter_id"`
	Prefix  string `json:"prefix"`
	Model   string `json:"model"` // model from the request body; buckets per-model usage
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
	model := strings.TrimSpace(req.Model)
	u, hasUsage := ParseUsage([]byte(req.Usage))

	// One atomic bump per request (so the control-plane's atomic drain never sees a
	// half-written request). Each metric is written both flat and, when the model is
	// known, as m:<metric>:<model> in the SAME hash — so a single HGETALL+DEL drain
	// carries both the aggregate and the per-model breakdown.
	_, _ = s.rdb.TxPipelined(ctx, func(p redis.Pipeliner) error {
		bump := func(metric string, n int64) {
			if n == 0 {
				return
			}
			p.HIncrBy(ctx, usageKey, metric, n)
			if model != "" {
				p.HIncrBy(ctx, usageKey, "m:"+metric+":"+model, n)
			}
		}
		bump("request_count", 1)
		if hasUsage {
			bump("prompt_tokens", int64(u.Prompt))
			bump("completion_tokens", int64(u.Completion))
			bump("total_tokens", int64(u.Total))
			// Cached ⊆ Prompt, already inside Total. Tracked separately so the control
			// plane can bill/rate-limit on total_tokens - cached_tokens.
			bump("cached_tokens", int64(u.Cached))
		}
		return nil
	})
	w.WriteHeader(http.StatusNoContent)
}

// ---- /models -------------------------------------------------------------
// GET /v1/models (proxied here by OpenResty): the gateway answers directly with
// the models THIS key may use — deployed models ∩ the key's allowed set — instead
// of proxying to a single engine (which only knows its own model). Key-gated via
// the Authorization header, same as /decide.

type modelObj struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	Created int64  `json:"created"`
	OwnedBy string `json:"owned_by"`
}

func (s *server) handleModels(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	secret := bearer(r.Header.Get("Authorization"))
	if secret == "" {
		writeErrJSON(w, 401, "missing api key")
		return
	}
	rec, ok, err := s.loadKey(ctx, sha256hex(secret))
	if err != nil {
		writeErrJSON(w, 503, "key store error")
		return
	}
	// Revoked/inactive can't list; active + rate_limited can (over-budget still shows
	// what the key is entitled to — only inference is blocked).
	if !ok || (rec.Status != "active" && rec.Status != "rate_limited") {
		writeErrJSON(w, 401, "unknown or revoked api key")
		return
	}

	deployed, err := s.listModels(ctx)
	if err != nil {
		writeErrJSON(w, 503, "route store error")
		return
	}
	created := time.Now().Unix()
	data := []modelObj{}
	for _, m := range deployed {
		if !rec.Models[m] { // same flat set the inference path uses (matches evaluate)
			continue
		}
		data = append(data, modelObj{ID: m, Object: "model", Created: created, OwnedBy: "frappe"})
	}
	writeJSON(w, map[string]any{"object": "list", "data": data})
}

// listModels returns every currently-deployed model (a deploy:<model> route key
// exists = ≥1 engine; empty sets are DEL'd by handleAdminRoutes). Deduped + sorted
// for stable output. /v1/models is low-frequency, so a SCAN per call is fine.
func (s *server) listModels(ctx context.Context) ([]string, error) {
	seen := map[string]bool{}
	var cursor uint64
	for {
		keys, next, err := s.rdb.Scan(ctx, cursor, "deploy:*", 200).Result()
		if err != nil {
			return nil, err
		}
		for _, k := range keys {
			seen[strings.TrimPrefix(k, "deploy:")] = true
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	out := make([]string, 0, len(seen))
	for m := range seen {
		out = append(out, m)
	}
	sort.Strings(out)
	return out, nil
}

func writeErrJSON(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"message": msg, "type": "grove_gateway"}})
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
	if am := strings.TrimSpace(h["models"]); am != "" {
		rec.Models = map[string]bool{}
		for _, m := range strings.Split(am, ",") {
			if m = strings.TrimSpace(m); m != "" {
				rec.Models[m] = true
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

// gatewayID names this gateway for request-ids: GROVE_GATEWAY_ID (set at deploy to the Proxy
// Server name) else the host's short name, else "gw".
func gatewayID() string {
	if v := strings.TrimSpace(os.Getenv("GROVE_GATEWAY_ID")); v != "" {
		return v
	}
	if h, err := os.Hostname(); err == nil && h != "" {
		return strings.SplitN(h, ".", 2)[0]
	}
	return "gw"
}

// cleanIDPart keeps a request-id part parseable: alnum stays, '-' becomes '_' (so the only
// '-' left is the separator), everything else is dropped.
func cleanIDPart(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '_':
			b.WriteRune(r)
		case r == '-':
			b.WriteByte('_')
		}
	}
	if b.Len() == 0 {
		return "x"
	}
	return b.String()
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return strings.Repeat("0", n*2)
	}
	return hex.EncodeToString(b)
}

func atoi(s string) int {
	n, _ := strconv.Atoi(strings.TrimSpace(s))
	return n
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
