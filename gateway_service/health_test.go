package main

import "testing"

// Which outcomes say the TARGET is broken, as opposed to the request being. Getting this wrong is
// expensive in both directions: too eager and one unplaced model retires a whole ingress, too
// reluctant and a dead ingress keeps taking traffic until someone notices.
func TestWhatCountsAsABrokenHop(t *testing.T) {
	for _, c := range []struct {
		name           string
		upstreamStatus string
		reason         string
		broken         bool
	}{
		{"no response at all", "", "", true},
		{"bad gateway", "502", "", true},
		{"gateway timeout", "504", "", true},
		{"ingress down", "503", "", true},
		// The one that matters. A healthy ingress with no replica for this model answers 503 and
		// says so; ejecting on it would take the ingress out for every OTHER model on it.
		{"healthy ingress, unplaced model", "503", "no-replica", false},
		{"served", "200", "", false},
		{"client asked for nonsense", "400", "", false},
		{"key refused downstream", "401", "", false},
		{"engine at capacity", "429", "", false},
		// vLLM's own 500 is the engine failing on one request, not the hop being unusable.
		{"engine error", "500", "", false},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := isHopFailure(c.upstreamStatus, c.reason); got != c.broken {
				t.Errorf("isHopFailure(%q, %q) = %v, want %v", c.upstreamStatus, c.reason, got, c.broken)
			}
		})
	}
}

func TestOnlyAServedRequestClearsTheCount(t *testing.T) {
	// A failure must not clear it, or a target alternating pass/fail would never reach the
	// threshold; and a blank status is not evidence of health.
	for _, status := range []string{"200", "404", "429"} {
		if !isHopSuccess(status) {
			t.Errorf("status %q should clear the failure count", status)
		}
	}
	for _, status := range []string{"", "502", "503", "504", "junk"} {
		if isHopSuccess(status) {
			t.Errorf("status %q should not clear the failure count", status)
		}
	}
}

func TestTheFirstUpstreamIsTheOneWeChose(t *testing.T) {
	// nginx writes a list when a request touched more than one upstream.
	for raw, want := range map[string]int{
		"502":       502,
		"502, 200":  502,
		"200 : 502": 200,
		"":          0,
		"-":         0,
		"junk":      0,
	} {
		if got := firstStatus(raw); got != want {
			t.Errorf("firstStatus(%q) = %d, want %d", raw, got, want)
		}
	}
}
