package main

import "testing"

func TestEvaluate(t *testing.T) {
	cases := []struct {
		name   string
		rec    KeyRecord
		model  string
		status int
	}{
		{"admit active key", KeyRecord{Status: "active"}, "m", 200},
		{"revoked key", KeyRecord{Status: "revoked"}, "m", 401},
		{"rate_limited (over monthly budget)", KeyRecord{Status: "rate_limited"}, "m", 429},
		{"model not allowed", KeyRecord{Status: "active", AllowedModels: map[string]bool{"a": true}}, "b", 403},
		{"model allowed", KeyRecord{Status: "active", AllowedModels: map[string]bool{"a": true}}, "a", 200},
		{"empty allowed set = all models", KeyRecord{Status: "active"}, "anything", 200},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := evaluate(tc.rec, tc.model)
			if got != tc.status {
				t.Fatalf("status = %d (%q), want %d", got, reason, tc.status)
			}
		})
	}
}

// rate_limited must win over the model gate: an over-budget key is rejected
// before we even check whether the model is allowed.
func TestEvaluateRateLimitPrecedence(t *testing.T) {
	rec := KeyRecord{Status: "rate_limited", AllowedModels: map[string]bool{"a": true}}
	if got, _ := evaluate(rec, "b"); got != 429 {
		t.Fatalf("expected 429 to win over 403, got %d", got)
	}
}
