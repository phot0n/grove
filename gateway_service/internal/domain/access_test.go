package domain

import "testing"

func set(models ...string) map[string]bool {
	m := map[string]bool{}
	for _, s := range models {
		m[s] = true
	}
	return m
}

// holder builds a current-shape user record — one that names a group, so canUse resolves rather
// than reading a flattened set.
func holder(group string) UserRecord {
	return UserRecord{Group: group}
}

func live(status string) KeyRecord { return KeyRecord{Status: status} }

func TestEvaluate(t *testing.T) {
	tier := GroupRecord{Models: set("a")}
	overBudget := UserRecord{Group: "tier", Limited: true}
	cases := []struct {
		name   string
		rec    KeyRecord
		usr    UserRecord
		model  string
		status int
	}{
		{"group grant admits", live("active"), holder("tier"), "a", 200},
		{"model the group does not grant", live("active"), holder("tier"), "b", 403},
		{"revoked key", live("revoked"), holder("tier"), "a", 401},
		{"holder over monthly budget", live("active"), overBudget, "a", 429},
		// The credential is the thing that is wrong, so it is named first.
		{"revoked beats over-budget", live("revoked"), overBudget, "a", 401},
		// Fails closed: no group and no allow must not fall through to "everything".
		{"ungrouped with no allow reaches nothing", live("active"), holder(""), "a", 403},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			grp := GroupRecord{}
			if tc.usr.Group != "" {
				grp = tier
			}
			got, reason := Evaluate(tc.rec, tc.usr, grp, tc.model)
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
		usr   UserRecord
		model string
		want  bool
	}{
		{"allow adds a model the group lacks", UserRecord{Group: "t", Allow: set("z")}, "z", true},
		{"allow works without any group", UserRecord{Allow: set("z")}, "z", true},
		{"deny beats the group's grant", UserRecord{Group: "t", Deny: set("b")}, "b", false},
		{"deny beats the user's own allow", UserRecord{Allow: set("z"), Deny: set("z")}, "z", false},
		{"denying an ungranted model is harmless", UserRecord{Group: "t", Deny: set("zzz")}, "a", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			grp := GroupRecord{}
			if tc.usr.Group != "" {
				grp = tier
			}
			if got := CanUse(tc.usr, grp, tc.model); got != tc.want {
				t.Fatalf("canUse = %v, want %v", got, tc.want)
			}
		})
	}
}

// Over budget must win over the model gate: the holder is rejected before we check whether the
// model was allowed, so the 429 is not masked by a 403.
func TestEvaluateRateLimitPrecedence(t *testing.T) {
	usr := UserRecord{Group: "tier", Limited: true}
	if got, _ := Evaluate(live("active"), usr, GroupRecord{Models: set("a")}, "b"); got != 429 {
		t.Fatalf("expected 429 to win over 403, got %d", got)
	}
}

// One person's budget is not the other keys' problem, and one leaked key is not the person's.
func TestTheKeyAndItsHolderAreJudgedSeparately(t *testing.T) {
	usr := holder("tier")
	tier := GroupRecord{Models: set("a")}
	if got, _ := Evaluate(live("revoked"), usr, tier, "a"); got != 401 {
		t.Fatalf("revoked key = %d, want 401", got)
	}
	if got, _ := Evaluate(live("active"), usr, tier, "a"); got != 200 {
		t.Fatalf("their other key = %d, want 200 — revoking one must not touch it", got)
	}
}

func TestPriorityComesFromTheGroup(t *testing.T) {
	if got := PriorityOf(holder("tier"), GroupRecord{Priority: -10}); got != -10 {
		t.Fatalf("priority = %d, want -10", got)
	}
	if got := PriorityOf(holder(""), GroupRecord{}); got != 0 {
		t.Fatalf("ungrouped priority = %d, want the baseline 0", got)
	}
}

// A key written before access moved onto the user still carries a group pointer and the holder's
// own lists. synthUser lifts them so the one decision path serves both shapes — which is what lets
// the control plane and the agent deploy in either order. Delete with the shim.
func TestAPreSplitKeyResolvesThroughSynthUser(t *testing.T) {
	rec := KeyRecord{
		Status: "active",
		Legacy: LegacyKey{HasGroup: true, Group: "tier", Allow: set("z"), Deny: set("b")},
	}
	tier := GroupRecord{Models: set("a", "b")}
	usr := SynthUser(rec)
	if got, reason := Evaluate(rec, usr, tier, "a"); got != 200 {
		t.Fatalf("group grant = %d (%q), want 200", got, reason)
	}
	if got, _ := Evaluate(rec, usr, tier, "z"); got != 200 {
		t.Fatalf("their own allow = %d, want 200", got)
	}
	if got, _ := Evaluate(rec, usr, tier, "b"); got != 403 {
		t.Fatalf("their own deny = %d, want 403", got)
	}
}

// The budget flag used to ride on the key's status. loadKey lifts it, and synthUser carries it, so
// an over-budget holder on a pre-split record still gets 429 rather than being read as revoked.
func TestAPreSplitBudgetFlagStillMeans429(t *testing.T) {
	rec := KeyRecord{Status: "active", Legacy: LegacyKey{HasGroup: true, Group: "tier", Limited: true}}
	if got, _ := Evaluate(rec, SynthUser(rec), GroupRecord{Models: set("a")}, "a"); got != 429 {
		t.Fatalf("status = %d, want 429", got)
	}
}

// A key written by a control plane that pushes user records carries no access of its own, so a
// missing user record must reach nothing rather than falling back to something.
func TestACurrentKeyWithNoUserRecordFailsClosed(t *testing.T) {
	rec := KeyRecord{Status: "active", User: "GU-1"}
	if rec.Legacy.HasProjection() {
		t.Fatal("a current key must not look like a legacy one")
	}
	// The zero user names no group, so the group it resolves is the zero one too.
	if got, _ := Evaluate(rec, UserRecord{}, GroupRecord{}, "a"); got != 403 {
		t.Fatalf("status = %d, want 403", got)
	}
}

// The other direction: a key still carrying the older projection is recognised as one, which is
// what keeps a box serving while the control plane it talks to is upgraded.
func TestAPreSplitKeyIsRecognisedAsOne(t *testing.T) {
	preGroup := KeyRecord{Legacy: LegacyKey{Models: set("a")}}
	preUser := KeyRecord{Legacy: LegacyKey{HasGroup: true, Group: "tier"}}
	ungrouped := KeyRecord{Legacy: LegacyKey{HasGroup: true}} // present-but-blank `group`
	for name, rec := range map[string]KeyRecord{
		"pre-group": preGroup, "pre-user": preUser, "pre-user ungrouped": ungrouped,
	} {
		if !rec.Legacy.HasProjection() {
			t.Fatalf("%s record was not recognised as legacy", name)
		}
	}
}

// A record written before the group split has no `group` field at all, so it still carries a
// flattened set and its own priority.
func TestPreGroupRecordStillResolves(t *testing.T) {
	rec := KeyRecord{Status: "active", Legacy: LegacyKey{Models: set("a"), Priority: -5}}
	usr := SynthUser(rec)
	if got, reason := Evaluate(rec, usr, GroupRecord{}, "a"); got != 200 {
		t.Fatalf("status = %d (%q), want 200", got, reason)
	}
	if got, _ := Evaluate(rec, usr, GroupRecord{}, "b"); got != 403 {
		t.Fatalf("status = %d, want 403 — a legacy set still fails closed", got)
	}
	if got := PriorityOf(usr, GroupRecord{Priority: -10}); got != -5 {
		t.Fatalf("priority = %d, want the record's own -5", got)
	}
}
