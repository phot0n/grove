package main

// Passive health, from traffic the gateway is already sending. No prober, no extra endpoint: every
// admitted request reports how its hop went at /meter, and a target that keeps failing stops being
// chosen. That is cheaper than active probing and strictly better informed — a probe tests a path
// no customer is on.

import (
	"context"
	"strconv"
	"strings"
	"time"
)

const (
	// How many consecutive failures retire a target. One is noise — a client disconnect, a single
	// unlucky reload. Three in a row against live traffic is a target that is not serving.
	ejectAfter = 3
	// How long a target stays retired with nothing further said about it. The counter is what
	// holds it out, so this is really "how long before we let traffic try again" — short, because
	// the only way back in is for a request to succeed, and no request is sent while it is out.
	healthTTL = 60 * time.Second
)

func healthKey(target string) string { return "health:" + target }

// recordOutcome moves one target's consecutive-failure count. A success clears it outright, so a
// target has to fail ejectAfter times in a row — not ejectAfter times ever — before it is dropped.
//
// The distinction that matters is which failures count. A connection error, a 502 or a 504 mean
// the hop itself is broken. A 503 carrying X-Grove-Reason: no-replica means the ingress answered
// perfectly well and one model has nowhere to go behind it; counting that would let a single
// unplaced model pull an ingress out of rotation for every other model on it.
func (s *server) recordOutcome(ctx context.Context, target, upstreamStatus, reason string) {
	if target == "" {
		return
	}
	if isHopFailure(upstreamStatus, reason) {
		key := healthKey(target)
		s.rdb.Incr(ctx, key)
		s.rdb.Expire(ctx, key, healthTTL)
		return
	}
	if isHopSuccess(upstreamStatus) {
		s.rdb.Del(ctx, healthKey(target))
	}
}

// isHopFailure reports whether this outcome says the target is broken, rather than that the
// request was.
func isHopFailure(upstreamStatus, reason string) bool {
	// A no-replica 503 is the ingress working correctly. Checked before the status, because the
	// status alone cannot tell it from an ingress that is down.
	if strings.TrimSpace(reason) == "no-replica" {
		return false
	}
	status := firstStatus(upstreamStatus)
	// Blank: nginx never got a response to record. The connection failed, timed out, or the
	// client vanished before the upstream answered — the first two are the target's fault and the
	// third is rare enough not to be worth distinguishing against a threshold of three.
	if status == 0 {
		return true
	}
	return status == 502 || status == 503 || status == 504
}

func isHopSuccess(upstreamStatus string) bool {
	status := firstStatus(upstreamStatus)
	return status > 0 && status < 500
}

// firstStatus reads nginx's $upstream_status, which is a comma-and-colon separated list when a
// request touched more than one upstream. The first entry is the one this gateway chose.
func firstStatus(raw string) int {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0
	}
	if i := strings.IndexAny(raw, ",:"); i >= 0 {
		raw = strings.TrimSpace(raw[:i])
	}
	status, err := strconv.Atoi(raw)
	if err != nil {
		return 0
	}
	return status
}

// markUnhealthy flips Healthy off on every route whose target has failed too often in a row.
// pickRoute's existing r.Healthy check does the rest.
//
// A Redis failure leaves every route as the control plane pushed it, which is the safe direction:
// ejection is an optimisation on top of a table that is already correct, and refusing to route
// because a health counter is unreadable would turn one broken Redis into an outage.
func (s *server) markUnhealthy(ctx context.Context, routes []Route) {
	if len(routes) == 0 {
		return
	}
	keys := make([]string, len(routes))
	for i, r := range routes {
		keys[i] = healthKey(r.EngineURL)
	}
	values, err := s.rdb.MGet(ctx, keys...).Result()
	if err != nil {
		return
	}
	for i, value := range values {
		text, ok := value.(string)
		if !ok {
			continue
		}
		if failures, err := strconv.Atoi(text); err == nil && failures >= ejectAfter {
			routes[i].Healthy = false
		}
	}
}
