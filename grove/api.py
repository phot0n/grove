# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Provisioning API (§7). The only user-facing surface: register a user, mint an
API key, and return a ready-to-use inference endpoint. Cold path (Frappe)."""

import frappe

from grove.grove.doctype.grove_user.grove_user import for_email

# TODO: we'll give central user some kind of "system" role

@frappe.whitelist()
def provision_key(name: str, email: str, token_limit: int=None, allowed_models: list[str]=None):
	"""Register the user (new/existing) + mint a key + return an OpenAI-compatible
	endpoint. Requires an authenticated session (mints credentials — §7)."""

	# 1. Resolve / register the user (idempotent on email).
	if not frappe.db.exists("User", email):
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = name
		user.enabled = 1
		user.send_welcome_email = 0
		user.user_type = "Website User"
		user.insert()

	# 2. Access and budget are per-user now, so both land on the Grove User rather than
	# the key. The budget is SHARED by every key this user holds — minting a second key
	# does not hand out a second allowance. Written unconditionally: the key links to this
	# doc, and a blank one is the correct fail-closed default (no group, no allow).
	grove_user = _set_policy(email, allowed_models, token_limit)

	# 3. Mint the key (controller generates secret + hash, pushes to gateways).
	key = frappe.new_doc("Grove API Key")
	key.user = grove_user
	key.status = "active"
	key.insert()

	host = frappe.db.get_single_value("Grove Settings", "gateway_host")
	if not host:
		frappe.throw("Gateway Host is not found")

	return {
		"gateway_url": host,
		"api_key": key.get_password("api_secret"),
	}


@frappe.whitelist()
def revoke_key(api_key):
	"""Revoke by the full key (not the doc name): hash it → find by key_hash →
	flip status → revoked. Gateways drop it within the cache TTL."""
	from grove.grove.doctype.grove_api_key.grove_api_key import hash_secret

	key = frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(api_key.strip())})
	if not key:
		frappe.throw("no such API key", frappe.DoesNotExistError)

	frappe.get_doc("Grove API Key", key).revoke()
	return "Revoked. Might take some time to reflect."


@frappe.whitelist()
def usage(users, month=None):
	from frappe.utils import now_datetime

	if not month:
		month = now_datetime().strftime("%Y-%m")

	if isinstance(users, str):
		users = [users]

	fields = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "request_count")
	# In and out by email; the records themselves are keyed by Grove User.
	emails = dict(
		frappe.get_all("Grove User", {"user": ("in", users)}, ["name", "user"], as_list=True)
	)
	records = frappe.get_all(
		"Usage Record",
		filters={"user": ["in", list(emails)], "month": month},
		fields=["name", "user", *fields],
	)

	usage = {}
	for r in records:
		# A user holds several keys and so several records a month — accumulate, don't
		# overwrite.
		totals = usage.setdefault(emails[r.user], dict.fromkeys(fields, 0))
		for f in fields:
			totals[f] += r.get(f) or 0
		totals["billable_tokens"] = totals["total_tokens"] - totals["cached_tokens"]

	# Same tokens, split by which model spent them. The gateway meters per model
	# (m:<metric>:<model>), so every record carries the breakdown as child rows.
	names = [r.name for r in records]
	model_rows = frappe.get_all(
		"Usage Model Row",
		filters={"parenttype": "Usage Record", "parent": ("in", names)},
		fields=["model", *fields],
	) if names else []
	model_summary = totals_by_model(model_rows, fields)
	return {"users": users, "month": month, "model_summary": model_summary, **usage}


def totals_by_model(rows, fields):
	"""Usage Model Rows folded into one entry per model, biggest consumer first. Rows arrive
	one per (record, model) — a user holds several keys, each with its own monthly record — so
	a model is summed across all of them rather than overwritten."""
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(row["model"], {"model": row["model"], **dict.fromkeys(fields, 0)})
		for f in fields:
			totals[f] += row.get(f) or 0

	summary = []
	for totals in per_model.values():
		totals["billable_tokens"] = totals["total_tokens"] - totals["cached_tokens"]
		summary.append(totals)
	summary.sort(key=lambda t: t["total_tokens"], reverse=True)
	return summary


@frappe.whitelist()
def available_models():
	return frappe.get_all(
		"Model",
		{"published": 1},
		["name", "display_name", "is_embedding"],
	)


def _set_policy(email, models, token_limit):
	"""Write the user's Grove User policy and return its name — the id every key, usage
	record and access lookup carries. `models` is exactly what they may call; `token_limit`
	is their shared monthly budget."""
	name = for_email(email)
	doc = frappe.get_doc("Grove User", name) if name else frappe.new_doc("Grove User")
	doc.user = email
	if models:
		doc.allow = []
		for model in models:
			doc.append("allow", {"model": model})
	if token_limit:
		doc.max_tokens = token_limit
	doc.save(ignore_permissions=True)
	return doc.name
