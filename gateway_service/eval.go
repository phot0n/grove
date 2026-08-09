package main

// KeyRecord is what the agent reads out of key:<sha256(secret)>. Deliberately thin: a credential's
// only fact of its own is whether it has been revoked. Who holds it, what they may call and
// whether they are over budget are facts about the PERSON and live in user:<name> — so one
// leaked key dies without touching the rest, and a budget flip is one write however many keys
// they hold.
type KeyRecord struct {
	Status    string // "active" | "revoked"
	User      string // Grove User doc name — the pointer to the UserRecord below
	KeyPrefix string // display id, for logs and usage attribution

	// Read only when user:<User> is absent, i.e. a record written before access moved off the
	// credential. synthUser turns it into a UserRecord so the decision path stays single.
	Legacy legacyKey
}

// legacyKey is quarantined so the whole pre-split projection can be deleted in one go, once every
// site has resynced against a control plane that pushes user records.
type legacyKey struct {
	HasGroup bool            // a pre-GROUP record wrote no `group` field at all, blank included
	Group    string          //
	Allow    map[string]bool //
	Deny     map[string]bool //
	Limited  bool            // the monthly-budget flag, which used to ride on Status
	Models   map[string]bool // pre-group: access already resolved to one flat set
	Priority int             // pre-group: the resolved priority
}

// hasProjection reports whether this key was written by a control plane that put access on the
// credential. A current one writes none of these fields, so a current key with no user record
// resolves to nothing rather than falling back to something — fail closed.
func (l legacyKey) hasProjection() bool {
	return l.HasGroup || l.Models != nil
}

// UserRecord is the holder's policy, stored under user:<Grove User name>. One record however many
// keys they hold. A user the control plane has not pushed reads back as the zero value, which
// grants nothing.
type UserRecord struct {
	Email   string          // denormalized, for humans reading Redis; no decision reads it
	Group   string          // Grove User Group name; "" = ungrouped (grants nothing by itself)
	Allow   map[string]bool // models this user may call on top of the group's
	Deny    map[string]bool // models this user may not call, whatever granted them
	Limited bool            // over their monthly token budget → 429

	// Set only by synthUser off a pre-group key, where the control plane had already resolved
	// access down to one model set. Nothing read from Redis sets it.
	Flattened bool
	Models    map[string]bool
	Priority  int
}

// synthUser builds the UserRecord a pre-split key record implies. Called only when user:<name> is
// missing, which is what makes the control plane and the agent deployable in either order.
func synthUser(rec KeyRecord) UserRecord {
	return UserRecord{
		Group:     rec.Legacy.Group,
		Allow:     rec.Legacy.Allow,
		Deny:      rec.Legacy.Deny,
		Limited:   rec.Legacy.Limited,
		Flattened: !rec.Legacy.HasGroup,
		Models:    rec.Legacy.Models,
		Priority:  rec.Legacy.Priority,
	}
}

// canUse is the access decision: the group's grant plus the user's own Allow, minus their Deny.
// Deny wins over every grant. Fails closed — no group and no Allow reaches nothing.
func canUse(usr UserRecord, grp GroupRecord, model string) bool {
	if usr.Flattened {
		return usr.Models[model] // legacy record: the control plane already resolved it
	}
	if usr.Deny[model] {
		return false
	}
	return grp.Models[model] || usr.Allow[model]
}

// GroupRecord is what a Grove User Group grants everyone in it, stored under group:<name>. A group
// the control plane has not pushed (or has removed) reads back as the zero value, which grants
// nothing and sits at the baseline priority.
type GroupRecord struct {
	Priority int             // vLLM `priority` to stamp on the body; lower is served first
	Models   map[string]bool // models the group grants
}

// priorityOf is the number stamped on an admitted body. It belongs to the group — there is no
// per-user override — so an ungrouped user sits at the baseline 0.
func priorityOf(usr UserRecord, grp GroupRecord) int {
	if usr.Flattened {
		return usr.Priority
	}
	return grp.Priority
}

// evaluate is the pure admission decision. Returns an HTTP status (200 = admit) and a short reason.
// Rate limiting is a per-user monthly TOKEN BUDGET enforced by the control plane
// (grove.usage_pull), which flags the USER when they are over — the gateway keeps no counters of
// its own, it just honors the pushed flag. Kept side-effect-free so it's unit-testable without
// Redis.
//
// The credential is checked before the budget: a revoked key is 401 even for a user who is also
// over their quota, because the key is the thing that is wrong.
func evaluate(rec KeyRecord, usr UserRecord, grp GroupRecord, model string) (int, string) {
	if rec.Status != "active" {
		return 401, "key revoked or inactive"
	}
	if usr.Limited {
		return 429, "monthly token quota exhausted"
	}
	if !canUse(usr, grp, model) {
		return 403, "access not allowed for model " + model
	}
	return 200, ""
}
