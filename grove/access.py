# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Who may call which Model. Every group a user belongs to grants, their own Allow adds, their Deny
removes, and nothing else is reachable.

The precedence is NOT applied here: Grove pushes each group, each user and each key as separate
Redis records, and the GATEWAY resolves the three at request time. That is what stops a one-row edit
on a group from invalidating every key beneath it."""

import frappe


def model_rows(parenttype, parents=None):
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
	filters = {"parenttype": "Grove User"}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all("Model Group Row", filters=filters, fields=["parent", "model_group"])
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, set()).add(row.model_group)
	return {parent: sorted(names) for parent, names in grouped.items()}
