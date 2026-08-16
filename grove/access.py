# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Who may call which Model. A user's group grants, their own Allow adds, their Deny removes, and
nothing else is reachable — a user with no grant gets no models.

The precedence itself is NOT applied here: Grove pushes the group as its own Redis record
(group:<name>), the user as theirs (user:<name>) pointing at it, and each key as a pointer to the
user; the gateway resolves the three at request time (pathway, internal/domain/access.go
`Evaluate`). That is what stops
a one-row edit on a group or a user from invalidating every key beneath it."""

import frappe


def model_rows(parenttype, parents=None):
	"""`{parent: {parentfield: sorted models}}` for every Grove Model Row under `parenttype`, or
	just `parents`. One query however many parents — reading a parent's rows one at a time is what
	used to make a full sync cost a round trip per user."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all(
		"Grove Model Row", filters=filters, fields=["parent", "model", "parentfield"]
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, {}).setdefault(row.parentfield, []).append(row.model)
	for fields in grouped.values():
		for models in fields.values():
			models.sort()
	return grouped
