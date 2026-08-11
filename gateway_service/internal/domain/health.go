package domain

import (
	"strconv"
	"strings"
)

// Passive health, from traffic already flowing: every admitted request reports how its hop went and
// a target that keeps failing stops being chosen. Better informed than a probe, which tests a path
// no customer is on.

const (
	// EjectAfter is how many consecutive failures retire a target. One is noise — a client
	// disconnect, a single unlucky reload. Three in a row against live traffic is a target that is
	// not serving.
	EjectAfter = 3
)

// IsHopFailure reports whether the TARGET is broken rather than the request. A connection error,
// 502 or 504 is the hop. A 503 with X-Grove-Reason: no-replica is not — the ingress answered fine
// and one model has nowhere to go, and counting it would eject the ingress for every other model.
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

// firstStatus reads an upstream status. nginx wrote a comma-separated list when a request touched
// several upstreams; the Go proxy reports one, so the split is vestigial and costs one IndexAny.
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
