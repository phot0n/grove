# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Provisioning API. The only user-facing surface: register a user, mint an API key, etc."""

import hmac

import frappe

from grove.grove.doctype.grove_user.grove_user import for_email, register_user
from grove.utils import utc_today

CONTROL_ROLE = "Grove Control"
ALLOWED_ROLES = [CONTROL_ROLE]
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens")
# What a caller sees as prompt tokens: every prompt-side counter, cached or written or plain.
PROMPT_COUNTERS = ("input_tokens", "cached_tokens", "cache_write_tokens", "cache_write_1h_tokens")


@frappe.whitelist()
def provision_key(name: str, email: str, geography: str, allowed_models: list[str]=None, pin: bool=False, free: bool=False):
	"""Register the user and mint a key for `geography`'s endpoint. `pin` also refuses the user
	everywhere else; `free` ignores pricing for them — otherwise they are prepaid and blocked until
	credited."""
	frappe.only_for(ALLOWED_ROLES)
	# Blank would read as no filter and hand out whichever endpoint comes first.
	host = frappe.db.get_value("Geography", geography, "endpoint") if geography else None
	if not host:
		frappe.throw(f"No Geography named {geography!r}.")

	# Access is per-user, so it lands on the Grove User rather than the key. Written
	# unconditionally: a blank one is the correct fail-closed default.
	grove_user = _set_policy(email, name, allowed_models, geography if pin else None, free)

	# The controller generates the secret and hash, and pushes to the gateways.
	key = frappe.new_doc("Grove API Key")
	key.user = grove_user
	key.status = "active"
	key.insert()

	return {
		"gateway_url": f"https://{host}",
		"api_key": key.get_password("api_secret"),
	}


@frappe.whitelist()
def revoke_key(api_key: str):
	"""Revoke by the full key, not the doc name. The row stays as the record it existed; a revoked
	key is no longer projected, so the next sync prunes it from every proxy."""
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
	"""Tokens per user and per model for `month` (YYYY-MM, UTC), summed over its day records."""
	frappe.only_for(ALLOWED_ROLES)
	from frappe.utils import get_first_day, get_last_day

	month = month or utc_today().strftime("%Y-%m")
	first = get_first_day(f"{month}-01")
	if isinstance(users, str):
		users = [users]

	# In and out by email; the records themselves are keyed by Grove User.
	emails = dict(
		frappe.get_list("Grove User", {"user": ("in", users)}, ["name", "user"], as_list=True)
	)
	records = frappe.get_list(
		"Usage Record",
		filters={"user": ["in", list(emails)], "day": ["between", [first, get_last_day(first)]]},
		fields=["name", "user"],
	)
	counter_rows = frappe.get_list(
		"Usage Counter Row",
		filters={"parenttype": "Usage Record", "parent": ("in", [r.name for r in records])},
		fields=["parent", "model", "counter", "amount"],
		parent_doctype="Usage Record",
	) if records else []
	email_of = {r.name: emails[r.user] for r in records}
	usage = _totals_by_user(counter_rows, email_of)
	model_summary = _totals_by_model(_token_rows(counter_rows), USAGE_FIELDS)
	return {"users": users, "month": month, "model_summary": model_summary, **usage}


@frappe.whitelist()
def available_models():
	"""Every model with a live route. A catalogue, not an entitlement list — the gateway is
	what enforces which of these a given API key may actually call."""
	frappe.only_for(ALLOWED_ROLES)
	return frappe.get_list(
		"Model",
		{"published": 1},
		["name", "model_id", "modality"],
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


def _set_policy(email, full_name, models, geography=None, free=False):
	"""Write the user's Grove User policy and return its name — the id every key, usage
	record and access lookup carries. `models` is exactly what they may call; `geography`, when
	given, pins them; `free`, when given, waives pricing. `full_name` names the login when this is
	the insert that creates it."""
	name = for_email(email)
	doc = frappe.get_doc("Grove User", name) if name else frappe.new_doc("Grove User")
	doc.user = register_user(email, full_name)
	if models:
		doc.allow = []
		for model in models:
			doc.append("allow", {"model": model})
	if geography:
		doc.geography = geography
	if free:
		doc.free = 1
	doc.save()
	return doc.name


def _token_rows(counter_rows):
	"""Counter rows re-shaped as the token columns the report has always returned, one row per
	model: prompt is every prompt-side counter, cached and completion their own."""
	rows = {}
	for row in counter_rows:
		totals = rows.setdefault(row["model"], {"model": row["model"], **dict.fromkeys(USAGE_FIELDS, 0)})
		amount = row.get("amount") or 0
		if row["counter"] in PROMPT_COUNTERS:
			totals["prompt_tokens"] += amount
		if row["counter"] in ("cached_tokens", "completion_tokens"):
			totals[row["counter"]] += amount
	return list(rows.values())


def _totals_by_user(counter_rows, email_of):
	"""{email: token totals} — every user with a record that month, zeros if their rows are empty.
	A user holds several keys and a record a day, so rows accumulate rather than overwrite."""
	by_user = {}
	for row in counter_rows:
		by_user.setdefault(email_of[row["parent"]], []).append(row)
	usage = {email: dict.fromkeys(USAGE_FIELDS, 0) for email in email_of.values()}
	for email, rows in by_user.items():
		for model_row in _token_rows(rows):
			for f in USAGE_FIELDS:
				usage[email][f] += model_row[f]
	return usage


def _totals_by_model(rows, fields):
	"""Token rows folded into one entry per model, biggest consumer first. Rows arrive one per
	(record, model) — a user holds several keys, each with its own day records — so a model is
	summed across all of them rather than overwritten."""
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(
			row["model"], {"model": row["model"], **dict.fromkeys(fields, 0)}
		)
		for f in fields:
			totals[f] += row.get(f) or 0

	return sorted(per_model.values(), key=lambda totals: -totals["prompt_tokens"])
