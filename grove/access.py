# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Who may call which Model. A user's group grants, their own Allow adds, their Deny removes, and
nothing else is reachable — a user with no grant gets no models.

The precedence itself is NOT applied here: Grove pushes the group as its own Redis record
(group:<name>) and each key as a pointer to it plus that user's deltas, and the gateway resolves
the three at request time (gateway_service/eval.go). That is what stops a one-row edit on a group
from invalidating every key its members hold."""

import frappe


def vllm_priority(group_priority):
	"""The `priority` the gateway stamps on a request, from a group's Priority.

	The two conventions are opposites, so the sign flips here and nowhere else: Grove
	stores "higher = more important", which is how an operator reads a tier, while vLLM
	serves the LOWEST number first. Baseline 0 is what an ungrouped user gets, so a group
	above 0 jumps ahead of them and one below 0 falls behind."""
	return -(group_priority or 0)


def key_access(grove_user):
	"""What one Grove User's keys carry: `(group, allow, deny)`. The group name is the pointer the
	gateway resolves; allow and deny are this user's own deltas on top of it. No Grove User doc
	means no group and no lists, so nothing is reachable."""
	if not grove_user:
		return "", [], []

	group = frappe.db.get_value("Grove User", grove_user, "user_group") or ""
	rows = frappe.get_all(
		"Grove Model Row",
		filters={"parenttype": "Grove User", "parent": grove_user},
		fields=["model", "parentfield"],
	)
	return (
		group,
		sorted(r.model for r in rows if r.parentfield == "allow"),
		sorted(r.model for r in rows if r.parentfield == "deny"),
	)
