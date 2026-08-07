package main

import "testing"

func set(models ...string) map[string]bool {
	m := map[string]bool{}
	for _, s := range models {
		m[s] = true
	}
	return m
}

// grouped builds a current-shape record — one that names a group, so canUse resolves rather than
// reading a flattened set.
func grouped(status, group string) KeyRecord {
	return KeyRecord{Status: status, Group: group, HasGroup: true}
}

func TestEvaluate(t *testing.T) {
	tier := GroupRecord{Models: set("a")}
	cases := []struct {
		name   string
		rec    KeyRecord
		grp    GroupRecord
		model  string
		status int
	}{
		{"group grant admits", grouped("active", "tier"), tier, "a", 200},
		{"model the group does not grant", grouped("active", "tier"), tier, "b", 403},
		{"revoked key", grouped("revoked", "tier"), tier, "a", 401},
		{"rate_limited (over monthly budget)", grouped("rate_limited", "tier"), tier, "a", 429},
		// Fails closed: no group and no allow must not fall through to "everything".
		{"ungrouped with no allow reaches nothing", grouped("active", ""), GroupRecord{}, "a", 403},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := evaluate(tc.rec, tc.grp, tc.model)
			if got != tc.status {
				t.Fatalf("status = %d (%q), want %d", got, reason, tc.status)
			}
		})
	}
}

// The user's own lists are deltas on top of the group — the precedence that used to be resolved in
// grove/access.py before the group moved into its own Redis record.
func TestCanUseResolvesTheUsersDeltas(t *testing.T) {
	tier := GroupRecord{Models: set("a", "b")}
	cases := []struct {
		name  string
		rec   KeyRecord
		model string
		want  bool
	}{
		{"allow adds a model the group lacks", KeyRecord{HasGroup: true, Allow: set("z")}, "z", true},
		{"allow works without any group", KeyRecord{HasGroup: true, Allow: set("z")}, "z", true},
		{"deny beats the group's grant", KeyRecord{HasGroup: true, Group: "t", Deny: set("b")}, "b", false},
		{"deny beats the user's own allow", KeyRecord{HasGroup: true, Allow: set("z"), Deny: set("z")}, "z", false},
		{"denying an ungranted model is harmless", KeyRecord{HasGroup: true, Group: "t", Deny: set("zzz")}, "a", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			grp := GroupRecord{}
			if tc.rec.Group != "" {
				grp = tier
			}
			if got := canUse(tc.rec, grp, tc.model); got != tc.want {
				t.Fatalf("canUse = %v, want %v", got, tc.want)
			}
		})
	}
}

// rate_limited must win over the model gate: an over-budget key is rejected
// before we even check whether the model is allowed.
func TestEvaluateRateLimitPrecedence(t *testing.T) {
	rec := grouped("rate_limited", "tier")
	if got, _ := evaluate(rec, GroupRecord{Models: set("a")}, "b"); got != 429 {
		t.Fatalf("expected 429 to win over 403, got %d", got)
	}
}

func TestPriorityComesFromTheGroup(t *testing.T) {
	rec := grouped("active", "tier")
	if got := priorityOf(rec, GroupRecord{Priority: -10}); got != -10 {
		t.Fatalf("priority = %d, want -10", got)
	}
	if got := priorityOf(grouped("active", ""), GroupRecord{}); got != 0 {
		t.Fatalf("ungrouped priority = %d, want the baseline 0", got)
	}
}

// A record written before the group split has no `group` field at all, so it still carries a
// flattened set and its own priority. The agent is deployed ahead of the control plane, and this is
// what keeps every live key working in between. Delete with the shim.
func TestLegacyRecordStillResolves(t *testing.T) {
	legacy := KeyRecord{Status: "active", Models: set("a"), Priority: -5}
	if got, reason := evaluate(legacy, GroupRecord{}, "a"); got != 200 {
		t.Fatalf("status = %d (%q), want 200", got, reason)
	}
	if got, _ := evaluate(legacy, GroupRecord{}, "b"); got != 403 {
		t.Fatalf("status = %d, want 403 — a legacy set still fails closed", got)
	}
	if got := priorityOf(legacy, GroupRecord{Priority: -10}); got != -5 {
		t.Fatalf("priority = %d, want the record's own -5", got)
	}
}
