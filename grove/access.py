# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Who may call which Model. The user's group grants, their own Allow adds, their Deny
removes, and nothing else is reachable — a user with no grant gets no models. Resolved here
and projected onto each API Key at sync time, so the gateway carries a flat set and holds no
precedence logic of its own."""

import frappe


def resolve(group_models, allow, deny):
	"""Model ids a user may call. Deny wins over every grant."""
	return (set(allow) | set(group_models)) - set(deny)


def vllm_priority(group_priority):
	"""The `priority` the gateway stamps on a request, from a group's Priority.

	The two conventions are opposites, so the sign flips here and nowhere else: Grove
	stores "higher = more important", which is how an operator reads a tier, while vLLM
	serves the LOWEST number first. Baseline 0 is what an ungrouped user gets, so a group
	above 0 jumps ahead of them and one below 0 falls behind."""
	return -(group_priority or 0)


def effective_models(grove_user):
	"""resolve() for one Grove User (by doc name — what a key carries), read off its group
	and its own lists. No Grove User doc means no group and no lists, so nothing is
	reachable."""
	if not grove_user:
		return set()

	group = frappe.db.get_value("Grove User", grove_user, "user_group")
	group_models = frappe.get_all(
		"Grove Model Row",
		filters={"parenttype": "Grove User Group", "parent": group},
		pluck="model",
	) if group else []

	rows = frappe.get_all(
		"Grove Model Row",
		filters={"parenttype": "Grove User", "parent": grove_user},
		fields=["model", "parentfield"],
	)
	return resolve(
		group_models,
		[r.model for r in rows if r.parentfield == "allow"],
		[r.model for r in rows if r.parentfield == "deny"],
	)


def effective_priority(grove_user):
	"""vllm_priority() for one Grove User (by doc name), read off their group. No group
	means the baseline 0 — priority is a group property, with no per-user override."""
	group = frappe.db.get_value("Grove User", grove_user, "user_group") if grove_user else None
	tier = frappe.db.get_value("Grove User Group", group, "priority") if group else 0
	return vllm_priority(tier)
