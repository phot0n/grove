# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Provisioning API (§7). The only user-facing surface: register a user, mint an
API key, and return a ready-to-use inference endpoint. Cold path (Frappe)."""

import hmac

import frappe

# Central authenticates as a dedicated, least-privilege control user (enrolled below).
CONTROL_USER = "central-control@frappe.cloud"
CONTROL_ROLE = "Central Control"


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
	from datetime import datetime, timezone

	if not month:
		month = datetime.now(timezone.utc).strftime("%Y-%m")

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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def enroll_control_client():
	"""Exchange the shared bootstrap secret (site config `control_bootstrap_secret`)
	for Central's own scoped API credential. Guest-auth — the secret is the only
	proof; re-enrolling rotates the key."""
	# Read from the raw request and drop it, so the secret is never stored in the Request Log.
	bootstrap_token = frappe.local.form_dict.pop("bootstrap_token", None)
	expected = frappe.conf.get("control_bootstrap_secret")

	# Constant-time compare; reject when the secret is unset or wrong.
	if not (expected and bootstrap_token) or not hmac.compare_digest(str(bootstrap_token), str(expected)):
		frappe.throw("Invalid bootstrap secret.", frappe.AuthenticationError)

	_ensure_control_role()
	user = _ensure_control_user()

	# Mint directly: generate_keys() would reject a Guest caller on permission.
	api_secret = frappe.generate_hash(length=15)
	user.api_key = user.api_key or frappe.generate_hash(length=15)
	user.api_secret = api_secret
	user.save(ignore_permissions=True)
	frappe.db.commit()  # persist before returning so Central can use the key immediately

	return {"api_key": user.api_key, "api_secret": api_secret, "user": user.name}


def _ensure_control_role():
	if not frappe.db.exists("Role", CONTROL_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": CONTROL_ROLE, "desk_access": 1}).insert(ignore_permissions=True)


def _ensure_control_user():
	# Least-privilege system user scoped to the control role; reused across enrolments.
	if frappe.db.exists("User", CONTROL_USER):
		return frappe.get_doc("User", CONTROL_USER)

	user = frappe.new_doc("User")
	user.email = CONTROL_USER
	user.first_name = "Central Control"
	user.user_type = "System User"
	user.send_welcome_email = 0
	user.enabled = 1
	user.append("roles", {"role": CONTROL_ROLE})
	user.insert(ignore_permissions=True)

	return user
