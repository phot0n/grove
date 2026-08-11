package domain

import (
	"strconv"
	"strings"
)

// Passive health, from traffic the gateway is already sending. No prober, no extra endpoint: every
// admitted request reports how its hop went, and a target that keeps failing stops being chosen.
// That is cheaper than active probing and strictly better informed — a probe tests a path no
// customer is on.

const (
	// EjectAfter is how many consecutive failures retire a target. One is noise — a client
	// disconnect, a single unlucky reload. Three in a row against live traffic is a target that is
	// not serving.
	EjectAfter = 3
)

// IsHopFailure reports whether this outcome says the target is broken, rather than that the
// request was.
//
// The distinction that matters is which failures count. A connection error, a 502 or a 504 mean
// the hop itself is broken. A 503 carrying X-Grove-Reason: no-replica means the ingress answered
// perfectly well and one model has nowhere to go behind it; counting that would let a single
// unplaced model pull an ingress out of rotation for every other model on it.
func IsHopFailure(upstreamStatus, reason string) bool {
	// A no-replica 503 is the ingress working correctly. Checked before the status, because the
	// status alone cannot tell it from an ingress that is down.
	if strings.TrimSpace(reason) == "no-replica" {
		return false
	}
	status := firstStatus(upstreamStatus)
	// Blank: no response was recorded. The connection failed, timed out, or the client vanished
	// before the upstream answered — the first two are the target's fault and the third is rare
	// enough not to be worth distinguishing against a threshold of three.
	if status == 0 {
		return true
	}
	return status == 502 || status == 503 || status == 504
}

func IsHopSuccess(upstreamStatus string) bool {
	status := firstStatus(upstreamStatus)
	return status > 0 && status < 500
}

// firstStatus reads an upstream status. Historically this was nginx's $upstream_status, a
// comma-and-colon separated list when a request touched more than one upstream; the first entry is
// the one this gateway chose. The Go proxy reports a single status, so the split is vestigial and
// costs one IndexAny.
//
// ponytail: the list form dies with the last OpenResty box; drop the split then.
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
