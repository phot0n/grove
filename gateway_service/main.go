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
	// How long a caller that names no session is pinned to one engine. 0 = not at all.
	syntheticTTL time.Duration
}

func main() {
	addr := env("GROVE_AGENT_ADDR", "127.0.0.1:9090")
	redisAddr := env("GROVE_REDIS_ADDR", "127.0.0.1:6379")

	// Before Redis: a missing token is a config fault, and needing a reachable Redis to hear
	// about it would report the wrong one.
	adminToken, err := requireAdminToken(os.Getenv("GROVE_ADMIN_TOKEN"))
	if err != nil {
		log.Fatal(err)
	}

	s := &server{
		rdb:          redis.NewClient(&redis.Options{Addr: redisAddr}),
		gatewayID:    gatewayID(),
		syntheticTTL: parseSyntheticTTL(os.Getenv("GROVE_SYNTHETIC_SESSION_TTL")),
	}
	if err := s.rdb.Ping(context.Background()).Err(); err != nil {
		log.Fatalf("redis unreachable at %s: %v", redisAddr, err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	mux.HandleFunc("/decide", s.handleDecide)
	mux.HandleFunc("/meter", s.handleMeter)
	mux.HandleFunc("/models", s.handleModels)
	// Control-plane push/pull surface (token-gated; reached via OpenResty /grove-admin/).
	mux.HandleFunc("/admin/keys", adminAuth(adminToken, s.handleAdminKeys))
	mux.HandleFunc("/admin/users", adminAuth(adminToken, s.handleAdminUsers))
	mux.HandleFunc("/admin/groups", adminAuth(adminToken, s.handleAdminGroups))
	mux.HandleFunc("/admin/routes", adminAuth(adminToken, s.handleAdminRoutes))
	mux.HandleFunc("/admin/usage", adminAuth(adminToken, s.handleAdminUsage))

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
	// Which placement was picked. Lua puts it on the access line, because the engine proxy made
	// $upstream_addr the box's :443 for every engine on it.
	Deployment string `json:"deployment,omitempty"`
	RequestID  string `json:"request_id,omitempty"` // gr-<gateway>-<deployment>-<keyprefix>-<rand>
	// Never omitempty: Lua stamps this on every admitted body, and a missing 0 would let a
	// client's own `priority` stand and elevate itself.
	Priority int `json:"priority"`
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

	// Who holds the key: their group, their own allow/deny, and whether they are over budget.
	usr, err := s.resolveUser(ctx, rec)
	if err != nil {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "user store error"})
		return
	}

	// What their group grants. Skipped for an ungrouped user, who reaches only their own Allow
	// list, so that request costs no third round trip.
	grp, err := s.loadGroup(ctx, usr.Group)
	if err != nil {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "group store error"})
		return
	}

	// Admission gates: key revoked, holder over monthly budget, then allowed models.
	if status, reason := evaluate(rec, usr, grp, req.Model); status != 200 {
		writeJSON(w, decideResp{Allow: false, Status: status, Reason: reason})
		return
	}

	// A caller that names a session keeps its engine, so its prefix cache stays warm. One that
	// names none is balanced — unless the operator asked for the synthetic session back, which
	// pins a whole key to one engine and is what a single-placement fleet always did.
	session, ttl := req.Session, stickyTTL
	if session == "" && s.syntheticTTL > 0 {
		session = sha256hex(meterID + "|" + req.Model)[:24]
		ttl = s.syntheticTTL
	}

	routes, err := s.loadRoutes(ctx, req.Model)
	if err != nil || len(routes) == 0 {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "model unavailable"})
		return
	}
	var stickyURL string
	if session != "" {
		stickyURL, _ = s.rdb.Get(ctx, "sticky:"+session).Result()
	}
	s.fillInFlight(ctx, routes)

	route, ok := pickRoute(routes, stickyURL)
	if !ok {
		writeJSON(w, decideResp{Allow: false, Status: 503, Reason: "no healthy server for model"})
		return
	}
	if session != "" {
		s.rdb.Set(ctx, "sticky:"+session, route.EngineURL, ttl)
	}
	requestID := s.buildRequestID(route, rec.KeyPrefix)
	s.claim(ctx, route.EngineURL, requestID)

	writeJSON(w, decideResp{
		Allow:       true,
		Status:      200,
		EngineURL:   route.EngineURL,
		InternalKey: route.InternalKey,
		MeterID:     meterID,
		Prefix:      rec.KeyPrefix,
		Session:     session,
		Deployment:  route.Deployment,
		RequestID:   requestID,
		Priority:    priorityOf(usr, grp),
	})
}

// buildRequestID stamps a traceable id on every admitted request:
// gr-<gateway>-<deployment>-<key prefix>-<random>. The key prefix is the API Key's unique doc
// name (already unique per key); the random tail makes each request unique. Parts are sanitized
// so the only '-' is the separator.
//
// The target is the Model Deployment, not the box: a box can serve one model from two
// deployments, and naming the box makes those two requests indistinguishable. Falls back to the
// server for routes pushed before the deployment field existed, then to a short hash of the
// engine URL — each tier a strictly worse name, none of them wrong.
func (s *server) buildRequestID(route Route, keyPrefix string) string {
	target := route.Deployment
	if target == "" {
		target = route.Server
	}
	if target == "" {
		target = sha256hex(route.EngineURL)[:8]
	}
	return fmt.Sprintf("gr-%s-%s-%s-%s",
		cleanIDPart(s.gatewayID), cleanIDPart(target), cleanIDPart(keyPrefix), randHex(16))
}

// ---- /meter --------------------------------------------------------------

type meterReq struct {
	MeterID string `json:"meter_id"`
	Prefix  string `json:"prefix"`
	Model   string `json:"model"` // model from the request body; buckets per-model usage
	Usage   string `json:"usage"` // raw JSON captured from the response (may be empty)
	// Which engine served it and under what request id — together they name the in-flight slot
	// this request claimed at /decide.
	EngineURL string `json:"engine_url"`
	RequestID string `json:"request_id"`
}

func (s *server) handleMeter(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req meterReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	// Released before the prefix check: a slot outlives the usage record it came with, and a key
	// record missing its prefix must not leave the engine counted as busy for the whole window.
	s.release(ctx, req.EngineURL, req.RequestID)
	if req.Prefix == "" {
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
// the models THIS key may use — deployed models ∩ what its group and its own
// allow/deny resolve to — instead of proxying to a single engine (which only knows
// its own model). Key-gated via the Authorization header, same as /decide.

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
		// No key at all → the public catalogue, so a prospect can see what is on offer before
		// signing up. A key that is present but wrong still 401s below: answering it with the
		// anonymous list would hide a broken key behind a shorter, plausible one.
		s.writePublicCatalog(w, r)
		return
	}
	rec, ok, err := s.loadKey(ctx, sha256hex(secret))
	if err != nil {
		writeErrJSON(w, 503, "key store error")
		return
	}
	// Revoked/inactive can't list. Over budget still can: it shows what the key is entitled to,
	// and that lives on the user, so only inference is blocked.
	if !ok || rec.Status != "active" {
		writeErrJSON(w, 401, "unknown or revoked api key")
		return
	}

	usr, err := s.resolveUser(ctx, rec)
	if err != nil {
		writeErrJSON(w, 503, "user store error")
		return
	}
	grp, err := s.loadGroup(ctx, usr.Group)
	if err != nil {
		writeErrJSON(w, 503, "group store error")
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
		if !canUse(usr, grp, m) { // same decision the inference path makes (matches evaluate)
			continue
		}
		data = append(data, modelObj{ID: m, Object: "model", Created: created, OwnedBy: "frappe"})
	}
	writeJSON(w, map[string]any{"object": "list", "data": data})
}

// catalogKey holds the pooled public catalogue: the comma list of models every group flagged
// Show in Public Catalogue names, assembled by the control plane and replaced whole on each
// groups push. Absent = no group is public, which is the default and answers an empty list.
const catalogKey = "catalog:public"

// writePublicCatalog answers an unauthenticated /v1/models with the models on offer.
//
// Names only, and still intersected with what is actually deployed — a catalogue that advertises
// a model no engine serves sends people to a 503. It grants nothing: canUse is untouched, so
// calling any of these without a key is still a 403 on the inference path.
func (s *server) writePublicCatalog(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	catalog, err := s.rdb.Get(ctx, catalogKey).Result()
	if err == redis.Nil {
		writeJSON(w, map[string]any{"object": "list", "data": []modelObj{}})
		return
	}
	if err != nil {
		writeErrJSON(w, 503, "catalog store error")
		return
	}
	public := modelSet(catalog)
	if len(public) == 0 {
		writeJSON(w, map[string]any{"object": "list", "data": []modelObj{}})
		return
	}

	deployed, err := s.listModels(ctx)
	if err != nil {
		writeErrJSON(w, 503, "route store error")
		return
	}
	writeJSON(w, map[string]any{"object": "list", "data": catalogObjects(public, deployed, time.Now().Unix())})
}

// catalogObjects is what an anonymous /v1/models answers: the advertised set intersected with
// what is deployed, in the order listModels already sorted them. Pure, so the rule that a
// catalogue never names a model no engine serves is testable without Redis.
func catalogObjects(public map[string]bool, deployed []string, created int64) []modelObj {
	data := []modelObj{}
	for _, m := range deployed {
		if public[m] {
			data = append(data, modelObj{ID: m, Object: "model", Created: created, OwnedBy: "frappe"})
		}
	}
	return data
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
	group, hasGroup := h["group"] // present-but-blank = ungrouped; absent = a pre-group record
	rec := KeyRecord{
		Status:    h["status"],
		User:      h["user"],
		KeyPrefix: h["prefix"],
		Legacy: legacyKey{
			HasGroup: hasGroup,
			Group:    strings.TrimSpace(group),
			Allow:    modelSet(h["allow"]),
			Deny:     modelSet(h["deny"]),
			Models:   modelSet(h["models"]),
		},
	}
	rec.Legacy.Priority, _ = strconv.Atoi(strings.TrimSpace(h["priority"])) // absent/garbage → 0
	// Status used to carry the holder's budget flag as a third value. Lift it off here, so
	// Status means only "is this credential live" — which is all a current record puts there.
	if rec.Status == "rate_limited" {
		rec.Status, rec.Legacy.Limited = "active", true
	}
	return rec, true, nil
}

// resolveUser is the second hop of the request path: key → user → group. A key whose user record
// is missing falls back to what the key itself carries, which is how a box still holding pre-split
// records keeps serving. A key with nothing to fall back to reaches nothing.
func (s *server) resolveUser(ctx context.Context, rec KeyRecord) (UserRecord, error) {
	if rec.User != "" {
		h, err := s.rdb.HGetAll(ctx, "user:"+rec.User).Result()
		if err != nil {
			return UserRecord{}, err
		}
		if len(h) > 0 {
			return UserRecord{
				Email:   h["email"],
				Group:   strings.TrimSpace(h["group"]),
				Allow:   modelSet(h["allow"]),
				Deny:    modelSet(h["deny"]),
				Limited: strings.TrimSpace(h["limited"]) == "1",
			}, nil
		}
	}
	if !rec.Legacy.hasProjection() {
		return UserRecord{}, nil
	}
	return synthUser(rec), nil
}

// loadGroup reads what a group grants. A group with no record — never pushed, or deleted — reads
// back as the zero value rather than an error, so a key pointing at one falls back to its own
// Allow list instead of 503ing.
func (s *server) loadGroup(ctx context.Context, name string) (GroupRecord, error) {
	if name == "" {
		return GroupRecord{}, nil
	}
	h, err := s.rdb.HGetAll(ctx, "group:"+name).Result()
	if err != nil {
		return GroupRecord{}, err
	}
	grp := GroupRecord{Models: modelSet(h["models"])}
	grp.Priority, _ = strconv.Atoi(strings.TrimSpace(h["priority"]))
	return grp, nil
}

// modelSet parses one of the comma-joined model lists the control plane writes. Blank → nil, which
// is a map that answers false to everything — the fail-closed default.
func modelSet(csv string) map[string]bool {
	csv = strings.TrimSpace(csv)
	if csv == "" {
		return nil
	}
	out := map[string]bool{}
	for _, m := range strings.Split(csv, ",") {
		if m = strings.TrimSpace(m); m != "" {
			out[m] = true
		}
	}
	return out
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

// requireAdminToken reads GROVE_ADMIN_TOKEN, which the agent refuses to start without. /admin is
// the only path the control plane has to this box, and treating a blank token as "admin off" left
// a box serving the keys and routes it last had, with nothing but one startup line to say why the
// pushes stopped landing. Whitespace is blank: an env file written from an empty template renders
// the value as spaces, and that is the same missing token.
func requireAdminToken(raw string) (string, error) {
	token := strings.TrimSpace(raw)
	if token == "" {
		return "", errors.New("GROVE_ADMIN_TOKEN is empty — refusing to start with an unguarded admin plane")
	}
	return token, nil
}

// parseSyntheticTTL reads GROVE_SYNTHETIC_SESSION_TTL: how long a caller that names no session of
// its own is pinned to the engine it first reached. Blank, zero or unparseable → 0, meaning no
// synthetic session at all and every such request balanced. Set it (30m restores what a
// single-placement fleet always did) to trade that balance for a warm prefix cache per key.
func parseSyntheticTTL(raw string) time.Duration {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0
	}
	ttl, err := time.ParseDuration(raw)
	if err != nil || ttl < 0 {
		log.Printf("GROVE_SYNTHETIC_SESSION_TTL %q is not a duration — synthetic sessions off", raw)
		return 0
	}
	return ttl
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
