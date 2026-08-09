package main

// The ingress half of the agent: pick a replica inside one VPC, and nothing else. No key store,
// no user or group lookup, no usage counters — an ingress is told a model and a session key and
// answers with an engine. The tenant it belongs to is not information this box has.
//
// Session affinity is stateless here, which is the whole reason the shared DNS name in front of a
// Network is safe. The gateway computes session_key and forwards it; every ingress in the Network
// holds the identical replica table and hashes it the same way, so whichever one DNS hands out
// returns the SAME replica and a client bouncing between ingresses keeps its prefix cache. Storing
// affinity per-ingress in Redis — the way the gateway's sticky: keys used to — would shred it.

import (
	"crypto/subtle"
	"encoding/json"
	"math"
	"net/http"
)

// spillFactor bounds how far above the mean in-flight a replica may go before a session hashing to
// it spills to the next in the ring. Plain consistent hashing pins a whale tenant onto one replica
// while its neighbours idle; the cache locality lost on a spilled request is far cheaper than a
// hot-spotted replica. 1.25 is loose enough that ordinary jitter does not spill.
const spillFactor = 1.25

type pickReq struct {
	// The bearer the gateway presented, forwarded by Lua. Proving the caller is a gateway, on a
	// hop already carrying verified server TLS and a firewall — so this is the third layer, not
	// the only one. Deliberately not the admin token: that is the control plane's credential,
	// and every gateway holding it would hand them the admin plane.
	Token string `json:"token"`
	Model string `json:"model"`
	// Computed by the gateway from the api key, the model and a jittered time bucket. Opaque
	// here: the ingress hashes it and never learns what went into it.
	SessionKey string `json:"session_key"`
	// The gateway's X-Request-Id, forwarded unchanged. Used as the in-flight slot's member so
	// /release can cross it off — never rewritten or extended, because the client was handed the
	// gateway's value and any mutation downstream breaks the correlation the id exists for.
	RequestID string `json:"request_id"`
}

type pickResp struct {
	Allow       bool   `json:"allow"`
	Status      int    `json:"status"`
	Reason      string `json:"reason,omitempty"`
	EngineURL   string `json:"engine_url,omitempty"`
	InternalKey string `json:"internal_key,omitempty"`
	// Stamped on the response as X-Grove-Engine so the gateway can attribute usage to a
	// placement it never chose, and written to this ingress's own access log.
	Deployment string `json:"deployment,omitempty"`
}

// handlePick is the ingress's whole data-path contribution: model in, engine out.
func (s *server) handlePick(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req pickReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, pickResp{Allow: false, Status: 400, Reason: "bad pick body"})
		return
	}
	if !s.isGateway(req.Token) {
		writeJSON(w, pickResp{Allow: false, Status: 401, Reason: "not-a-gateway"})
		return
	}

	routes, err := s.loadRoutes(ctx, req.Model)
	if err != nil || len(routes) == 0 {
		// no-replica, not unhealthy. The gateway must not eject a whole network over one model
		// that has nowhere to go inside it — see the Reason the Lua turns into X-Grove-Reason.
		writeJSON(w, pickResp{Allow: false, Status: 503, Reason: "no-replica"})
		return
	}
	s.fillInFlight(ctx, routes)

	route, status := pickReplica(routes, req.SessionKey)
	if status == 429 {
		writeJSON(w, pickResp{Allow: false, Status: 429, Reason: "at-capacity"})
		return
	}
	if status != 200 {
		writeJSON(w, pickResp{Allow: false, Status: 503, Reason: "no-replica"})
		return
	}
	s.claim(ctx, route.EngineURL, req.RequestID)

	writeJSON(w, pickResp{
		Allow:       true,
		Status:      200,
		EngineURL:   route.EngineURL,
		InternalKey: route.InternalKey,
		Deployment:  route.Deployment,
	})
}

// isGateway checks the data-path bearer in constant time. An ingress that was given no token
// refuses everything rather than waving callers through: a blank secret compared loosely is the
// shape where a misrendered env file silently opens the whole VPC's engines to anyone who can
// reach 443, and the firewall in front is not something this process can verify.
func (s *server) isGateway(token string) bool {
	if s.ingressToken == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(token), []byte(s.ingressToken)) == 1
}

// handleRelease crosses a finished request off its engine. The ingress's counterpart to /meter,
// minus the metering: usage belongs to a tenant, and the ingress has no idea which one.
func (s *server) handleRelease(w http.ResponseWriter, r *http.Request) {
	var req struct {
		EngineURL string `json:"engine_url"`
		RequestID string `json:"request_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	s.release(r.Context(), req.EngineURL, req.RequestID)
	w.WriteHeader(http.StatusNoContent)
}

// pickReplica chooses among a model's replicas in this Network: bounded-load rendezvous hashing on
// the session key, falling back to least-in-flight when there is no key.
//
// Rendezvous rather than a hash ring with virtual nodes: it gives the same minimal-disruption
// property — removing a replica only moves the keys that were on it — in a dozen lines and with no
// ring to build, and the order it produces depends on nothing but the key and the engine URLs. Two
// ingresses holding the same table therefore agree without exchanging a byte, which is what makes
// DNS balancing in front of them safe.
//
// Returns the status the caller should answer with, same vocabulary as pickRoute: 200 admit, 503
// the model has nowhere to go here, 429 every replica is at its --max-num-seqs.
func pickReplica(routes []Route, sessionKey string) (Route, int) {
	var free []Route
	healthy := 0
	for _, r := range routes {
		if !r.Healthy || r.EngineURL == "" {
			continue
		}
		healthy++
		if r.hasRoom() {
			free = append(free, r)
		}
	}
	if healthy == 0 {
		return Route{}, 503
	}
	if len(free) == 0 {
		return Route{}, 429
	}
	if sessionKey == "" {
		return leastInFlight(free), 200
	}

	// Every replica at or under the bound is an acceptable home for this key; the highest-scoring
	// of those wins. A fleet that is entirely idle has a bound of 0, so the +1 keeps the first
	// choice from spilling before anything is actually loaded.
	bound := int(math.Ceil(spillFactor*meanInFlight(free))) + 1
	best, bestScore, found := Route{}, uint64(0), false
	for _, r := range free {
		if r.InFlight > bound {
			continue
		}
		if score := rendezvousScore(sessionKey, r.EngineURL); !found || score > bestScore {
			best, bestScore, found = r, score, true
		}
	}
	if !found {
		// Everything is above the bound, which means the load is even and the bound is simply
		// tight. Take the least loaded rather than refusing a request every replica could serve.
		return leastInFlight(free), 200
	}
	return best, 200
}

func leastInFlight(routes []Route) Route {
	best := routes[0]
	for _, r := range routes[1:] {
		if r.InFlight < best.InFlight {
			best = r
		}
	}
	return best
}

func meanInFlight(routes []Route) float64 {
	total := 0
	for _, r := range routes {
		total += r.InFlight
	}
	return float64(total) / float64(len(routes))
}

// rendezvousScore weighs one replica for one session key. Any hash both ingresses compute
// identically will do; sha256 is already here and this is one hash per replica per request.
func rendezvousScore(sessionKey, engineURL string) uint64 {
	sum := sha256sum(sessionKey + "|" + engineURL)
	var score uint64
	for _, b := range sum[:8] {
		score = score<<8 | uint64(b)
	}
	return score
}
