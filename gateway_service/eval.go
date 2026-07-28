package main

// KeyRecord is the per-key state the agent needs to authorize + rate-limit a
// request. It's the value cached in Redis under key:<sha256(secret)> (v1: seeded
// directly; later: cache-aside from Grove's DB, §6 job 1).
type KeyRecord struct {
	Status    string          // "active" | "revoked" | "rate_limited"
	User      string          // Frappe User (denormalized)
	KeyPrefix string          // display id, for logs/usage attribution
	Models    map[string]bool // exactly what this key may call; empty = nothing
}

// evaluate is the pure admission decision. Returns an HTTP status (200 = admit)
// and a short reason. Rate limiting is a per-key monthly TOKEN BUDGET enforced by
// the control plane (grove.usage_pull), which flags a key status="rate_limited"
// when it's over — the gateway keeps no counters of its own, it just honors the
// pushed status. Kept side-effect-free so it's unit-testable without Redis.
func evaluate(rec KeyRecord, model string) (int, string) {
	if rec.Status == "rate_limited" {
		return 429, "monthly token quota exhausted"
	}
	if rec.Status != "active" {
		return 401, "key revoked or inactive"
	}
	// The control plane resolves group grants, per-user allow/deny and the model's
	// Public flag into this flat set (grove/access.py), so there is no precedence to
	// apply here. Empty means no access, never "everything" — access fails closed.
	if !rec.Models[model] {
		return 403, "access not allowed for model " + model
	}
	return 200, ""
}
