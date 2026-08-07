package main

// KeyRecord is the per-key state the agent needs to authorize a request. It's the value stored in
// Redis under key:<sha256(secret)>. Access is NOT flattened onto it: the key names its group, and
// carries only that user's own additions and removals on top of what the group grants.
type KeyRecord struct {
	Status    string          // "active" | "revoked" | "rate_limited"
	User      string          // Frappe User (denormalized)
	KeyPrefix string          // display id, for logs/usage attribution
	Group     string          // Grove User Group name; "" = ungrouped (grants nothing by itself)
	Allow     map[string]bool // models this user may call on top of the group's
	Deny      map[string]bool // models this user may not call, whatever granted them

	// Legacy projection — set only for a record written by a control plane that still flattened
	// access onto the key. HasGroup is what tells the two apart: a current record always writes
	// the `group` field, blank included. Drop both once every site is on the group push.
	HasGroup bool
	Models   map[string]bool
	Priority int
}

// GroupRecord is what a Grove User Group grants everyone in it, stored under group:<name>. A group
// the control plane has not pushed (or has removed) reads back as the zero value, which grants
// nothing and sits at the baseline priority.
type GroupRecord struct {
	Priority int             // vLLM `priority` to stamp on the body; lower is served first
	Models   map[string]bool // models the group grants
}

// canUse is the access decision: the group's grant plus the user's own Allow, minus their Deny.
// Deny wins over every grant. Fails closed — no group and no Allow reaches nothing.
func canUse(rec KeyRecord, grp GroupRecord, model string) bool {
	if !rec.HasGroup {
		return rec.Models[model] // legacy record: the control plane already resolved it
	}
	if rec.Deny[model] {
		return false
	}
	return grp.Models[model] || rec.Allow[model]
}

// priorityOf is the number stamped on an admitted body. It belongs to the group — there is no
// per-user override — so an ungrouped key sits at the baseline 0.
func priorityOf(rec KeyRecord, grp GroupRecord) int {
	if !rec.HasGroup {
		return rec.Priority
	}
	return grp.Priority
}

// evaluate is the pure admission decision. Returns an HTTP status (200 = admit) and a short reason.
// Rate limiting is a per-user monthly TOKEN BUDGET enforced by the control plane
// (grove.usage_pull), which flags a key status="rate_limited" when it's over — the gateway keeps no
// counters of its own, it just honors the pushed status. Kept side-effect-free so it's
// unit-testable without Redis.
func evaluate(rec KeyRecord, grp GroupRecord, model string) (int, string) {
	if rec.Status == "rate_limited" {
		return 429, "monthly token quota exhausted"
	}
	if rec.Status != "active" {
		return 401, "key revoked or inactive"
	}
	if !canUse(rec, grp, model) {
		return 403, "access not allowed for model " + model
	}
	return 200, ""
}
