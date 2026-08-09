# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Provisioning API (§7). The only user-facing surface: register a user, mint an
API key, and return a ready-to-use inference endpoint. Cold path (Frappe).

Every endpoint here is for the control client and calls frappe.only_for(CONTROL_ROLE) —
@frappe.whitelist() alone lets ANY logged-in user in, role or not. The Grove Control role
carries no doctype permissions (Grove's doctypes grant System Manager only), so it reaches
these methods and nothing else. enroll_control_client is the one exception: it runs before
the client has a session and is gated by the shared bootstrap secret instead."""

import hmac

import frappe

from grove.grove.doctype.grove_user.grove_user import for_email
from grove.grove.doctype.usage_record.usage_record import billable

CONTROL_ROLE = "Grove Control"
ALLOWED_ROLES = [CONTROL_ROLE]

@frappe.whitelist()
def provision_key(name: str, email: str, token_limit: int=None, allowed_models: list[str]=None):
	"""Register the user (new/existing) + mint a key + return an OpenAI-compatible
	endpoint. Control client only — it mints credentials (§7)."""
	frappe.only_for(ALLOWED_ROLES)

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

	# With a scheme, because the host alone is not a base URL an SDK can take. https always: a
	# proxy with a certificate 301s port 80, and one without has no business handing out keys.
	return {
		"gateway_url": f"https://{host}",
		"api_key": key.get_password("api_secret"),
	}


@frappe.whitelist()
def revoke_key(api_key: str):
	"""Revoke by the full key (not the doc name): hash it → find by key_hash → flip to revoked.
	The row stays as the record it existed; a Gateway Deletion is written alongside and the next
	sync drops the record from every proxy, so the key stops working within a tick."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.grove_api_key.grove_api_key import hash_secret

	key = frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(api_key.strip())})
	if not key:
		frappe.throw("no such API key", frappe.DoesNotExistError)

	frappe.get_doc("Grove API Key", key).revoke()
	return "Revoked. Might take some time to reflect."


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_control_client(email: str):
	# the secret should not be stored in the Request Log.
	token = frappe.form_dict.pop("token", None)
	expected = frappe.conf.get("control_secret")

	# Constant-time compare; reject when the secret is unset or wrong.
	if not (expected and token) or not hmac.compare_digest(str(token), str(expected)):
		frappe.throw("Invalid Operation", frappe.AuthenticationError)

	control_user = _create_control_user(email)

	# Mint directly: generate_keys() would reject a Guest caller on permission.
	api_secret = frappe.generate_hash(length=15)
	control_user.api_key = control_user.api_key or frappe.generate_hash(length=15)
	control_user.api_secret = api_secret
	control_user.save(ignore_permissions=True)

	return {"api_key": control_user.api_key, "api_secret": api_secret, "user": control_user.name}


@frappe.whitelist()
def create_control_client_key():
	frappe.only_for(ALLOWED_ROLES)

	control_user = frappe.get_doc("User", frappe.session.user)
	api_secret = frappe.generate_hash(length=15)
	control_user.api_key = control_user.api_key or frappe.generate_hash(length=15)
	control_user.api_secret = api_secret
	control_user.save(ignore_permissions=True)

	return {"api_key": control_user.api_key, "api_secret": api_secret, "user": control_user.name}

@frappe.whitelist()
def usage(users: list[str] | str, month: str = None):
	frappe.only_for(ALLOWED_ROLES)
	from frappe.utils import now_datetime

	if not month:
		month = now_datetime().strftime("%Y-%m")

	if isinstance(users, str):
		users = [users]

	_fields = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "request_count")
	# In and out by email; the records themselves are keyed by Grove User.
	emails = dict(
		frappe.get_all("Grove User", {"user": ("in", users)}, ["name", "user"], as_list=True)
	)
	records = frappe.get_all(
		"Usage Record",
		filters={"user": ["in", list(emails)], "month": month},
		fields=["name", "user", *_fields],
	)

	usage = {}
	for r in records:
		# A user holds several keys and so several records a month — accumulate, don't
		# overwrite.
		totals = usage.setdefault(emails[r.user], {"billable_tokens": 0, **dict.fromkeys(_fields, 0)})
		for f in _fields:
			totals[f] += r.get(f) or 0
		# Accumulated per record, not derived from the running totals: billable has a floor, and
		# applying it once at the end would let one malformed record cancel another key's usage.
		totals["billable_tokens"] += billable(r.get("total_tokens"), r.get("cached_tokens"))

	# Same tokens, split by which model spent them. The gateway meters per model
	# (m:<metric>:<model>), so every record carries the breakdown as child rows.
	names = [r.name for r in records]
	model_rows = frappe.get_all(
		"Usage Model Row",
		filters={"parenttype": "Usage Record", "parent": ("in", names)},
		fields=["model", *_fields],
	) if names else []
	model_summary = _totals_by_model(model_rows, _fields)
	return {"users": users, "month": month, "model_summary": model_summary, **usage}


@frappe.whitelist()
def available_models():
	"""Every model with a live route. A catalogue, not an entitlement list — the gateway is
	what enforces which of these a given API key may actually call."""
	frappe.only_for(ALLOWED_ROLES)
	return frappe.get_all(
		"Model",
		{"published": 1},
		["name", "display_name", "modality"],
	)


def _create_control_user(email):
	if frappe.db.exists("User", email):
		frappe.throw("Invalid Operation")

	doc = frappe.new_doc("User")
	doc.email = email
	doc.first_name = "Control Client"
	doc.user_type = "Website User"
	doc.send_welcome_email = 0
	doc.enabled = 1
	doc.append("roles", {"role": CONTROL_ROLE})
	doc.insert(ignore_permissions=True)
	return doc


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


def _totals_by_model(rows, fields):
	"""Usage Model Rows folded into one entry per model, biggest consumer first. Rows arrive
	one per (record, model) — a user holds several keys, each with its own monthly record — so
	a model is summed across all of them rather than overwritten."""
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(
			row["model"], {"model": row["model"], "billable_tokens": 0, **dict.fromkeys(fields, 0)}
		)
		for f in fields:
			totals[f] += row.get(f) or 0
		# Per row, for the same reason as above and so this agrees with usage_record.billable_tokens.
		totals["billable_tokens"] += billable(row.get("total_tokens"), row.get("cached_tokens"))

	return sorted(per_model.values(), key=lambda t: t["total_tokens"], reverse=True)
