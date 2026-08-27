# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Who may call which Model. Every group a user belongs to grants, their own Allow adds, their
Deny removes, and nothing else is reachable — a user with no grant gets no models.

The precedence itself is NOT applied here: Grove pushes each group as its own Redis record
(group:<name>), the user as theirs (user:<name>) naming the groups they are in, and each key as a
pointer to the user; the gateway unions the groups and resolves the three at request time (pathway,
internal/domain/access.go `Evaluate`). That is what stops a one-row edit on a group or a user from
invalidating every key beneath it."""

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


def group_rows(parents=None):
	"""`{parent: sorted group names}` for every Grove Group Row on a Grove User, or just `parents`.
	One query however many users, like `model_rows`. Deduped: two rows naming the same group are
	one membership, and the wire list is a set."""
	filters = {"parenttype": "Grove User"}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all("Grove Group Row", filters=filters, fields=["parent", "user_group"])
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, set()).add(row.user_group)
	return {parent: sorted(names) for parent, names in grouped.items()}
