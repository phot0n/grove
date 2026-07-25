# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Provisioning API (§7). The only user-facing surface: register a user, mint an
API key, and return a ready-to-use inference endpoint. Cold path (Frappe)."""

import frappe

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

	# 3. Mint the key (controller generates secret + hash, pushes to gateways).
	key = frappe.new_doc("API Key")
	key.user = email
	key.status = "active"
	key.allowed_models = ",".join(allowed_models) if allowed_models else None
	key.max_tokens = token_limit or 0
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
	from grove.grove.doctype.api_key.api_key import hash_secret

	key = frappe.db.get_value("API Key", {"key_hash": hash_secret(api_key.strip())})
	if not key:
		frappe.throw("no such API key", frappe.DoesNotExistError)

	frappe.get_doc("API Key", key).revoke()
	return "Revoked. Might take some time to reflect."


@frappe.whitelist()
def usage(users, month=None):
	from frappe.utils import now_datetime

	if not month:
		month = now_datetime().strftime("%Y-%m")

	if isinstance(users, str):
		users = [users]

	records = frappe.get_all(
		"Usage Record",
		filters={"user": ["in", users], "month": month},
		fields=["user", "prompt_tokens", "completion_tokens", "total_tokens", "request_count", "cached_tokens"],
	)

	usage = {}
	for r in records:
		usage[r.user] = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "request_count": 0}
		for f in usage[r.user]:
			usage[r.user][f] += r.get(f) or 0

		usage[r.user]["billable_tokens"] = usage[r.user]["total_tokens"] - usage[r.user]["cached_tokens"]
	return {"users": users, "month": month, **usage}


@frappe.whitelist()
def available_models():
	"""Return the list of published models."""
	return frappe.get_all("Model", {"published": 1}, ["name", "display_name"])
