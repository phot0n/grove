# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""The desired state a box is pushed, and the hash gate that decides which sections travel.

`groups` and `routes` travel whole. `users` and `keys` scale with customer count, so they split
into 256 buckets (`bucket_of`) hashed independently — one key minted re-pushes one bucket, not the
population. Absence prunes: a deleted group or revoked key stops being named and the agent
removes it."""

import hashlib
import json

import frappe

from grove.access import group_rows, model_rows
from grove.grove.doctype.gateway_spend.gateway_spend import spent_known
from grove.pathway import routes
from grove.pricing import micro_floor, nano


def effective_groups():
	"""Every Model Group projected for the gateway: what it grants. One record per group
	however many keys point at it — the reason the group is not flattened onto each key."""
	granted = model_rows("Model Group")
	return [
		{
			"name": name,
			"models": ",".join(granted.get(name, {}).get("models", [])),
		}
		for name in sorted(frappe.get_all("Model Group", pluck="name"))
	]


def effective_users():
	"""Every Grove User projected for the gateway. One record per person however many keys they
	hold — the reason none of this is flattened onto the keys.

	`limited` is the control plane's verdict, honoured as a 429. Holding it on the PERSON stops a
	blocked user minting a fresh key. Every user is prepaid unless marked Free, and carries `budget`,
	the ceiling the gateway refuses at on its own: what is left, before each Redis adds what it has
	already counted. The wire says `prepaid`, not `free`: a field absent on an old push must read
	as no gate."""
	deltas = model_rows("Grove User")
	memberships = group_rows()
	users = frappe.get_all(
		"Grove User", fields=["name", "user", "rate_limited", "log_payloads", "geography", "free", "balance"]
	)
	return [
		{
			"name": u.name,
			"email": u.user or "",  # for humans reading Redis; no decision reads it
			# One comma list: the gateway unions the grants per entry. Sorted, so the same
			# membership always hashes the same.
			"group": ",".join(memberships.get(u.name, [])),
			"allow": ",".join(deltas.get(u.name, {}).get("allow", [])),
			"deny": ",".join(deltas.get(u.name, {}).get("deny", [])),
			"limited": bool(u.rate_limited),
			# Opt-in to prompt/output logging. Customer content: absent or falsy stays off.
			"log_payloads": bool(u.get("log_payloads")),
			# Every gateway gets every user; one outside this pin answers 403. Blank = unpinned.
			"geography": u.get("geography") or "",
			"prepaid": not u.get("free"),
			"budget": 0 if u.get("free") else remaining_budget(u.get("balance")),
		}
		for u in sorted(users, key=lambda u: u.name)
	]


def remaining_budget(balance):
	"""The user's `balance` in nano-USD, floored to a whole µUSD so sub-µUSD truncation drift does
	not re-hash the user's bucket every pull."""
	return micro_floor(nano(balance or 0))


def users_for_redis(users, redis):
	"""Each prepaid user's ceiling on this Redis: what is left plus what the Redis has already
	counted, so `spent >= budget` there means its undrained spend exceeds the real balance."""
	known = spent_known(redis) if redis else {}
	return [
		{**user, "budget": user["budget"] + known.get(user["name"], 0)} if user["prepaid"] else user
		for user in users
	]


def effective_keys():
	"""Every LIVE API Key projected for the gateway. A key is a pointer to whoever holds it and
	nothing else — what they may call belongs to the person.

	Revoked keys are not projected: absent from their bucket, the push prunes them off every box.
	The row stays in Grove as the record of a credential that existed."""
	keys = frappe.get_all(
		"Grove API Key", filters={"status": "active"}, fields=["name", "key_hash", "user", "status"]
	)
	return [
		{
			"key_hash": k.key_hash,
			"prefix": k.name,  # doc name (random hash) = usage attribution id
			"user": k.user,  # Grove User doc name — the pointer to user:<name>
			"status": k.status or "active",
		}
		for k in sorted(keys, key=lambda k: k.key_hash or "")
		if k.key_hash
	]


def bucket_of(record_id):
	"""Which of the 256 state buckets a record belongs to. The agent prunes by the same rule, so
	the two sides must never disagree."""
	return hashlib.sha256(str(record_id).encode()).hexdigest()[:2]


def _hash(content):
	"""The agent stores this verbatim and never recomputes it, so only THIS function has to be
	deterministic — hence the sorts in the builders above."""
	return hashlib.sha256(
		json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def flat_section(content):
	return {**content, "hash": _hash(content)}


def bucketed_section(records, id_field):
	buckets = {}
	for record in records:
		buckets.setdefault(bucket_of(record[id_field]), []).append(record)
	return {"buckets": {
		label: {"records": rows, "hash": _hash({"records": rows})}
		for label, rows in buckets.items()
	}}


def gateway_snapshot(geography, redis=None, shared=None):
	"""The same for every gateway in `geography` on `redis`, so a run builds it once per pair:
	only the routes differ between geographies, only the prepaid ceilings between Redises. A run
	hands every call the same `shared` dict, so the user records are built once."""
	shared = {} if shared is None else shared
	if "users" not in shared:
		shared["users"] = effective_users()
	return {
		"groups": flat_section({"records": effective_groups()}),
		"users": bucketed_section(users_for_redis(shared["users"], redis), "name"),
		"keys": bucketed_section(effective_keys(), "key_hash"),
		"routes": flat_section({"table": routes.gateway_routes(geography)}),
	}


def gateway_geography(gateway):
	"""Blank for a gateway with none, which is then given no routes at all."""
	return frappe.db.get_value("Gateway Server", gateway, "geography") or ""


def gateway_redis(gateway):
	"""The Redis a gateway counts on: its Gateway Store, or itself for a box on its own."""
	return frappe.db.get_value("Gateway Server", gateway, "gateway_store") or gateway


def ingress_snapshot(ingress):
	"""Its replica table and nothing else — that plane has no keys, users or groups section."""
	return {"routes": flat_section({"table": routes.replicas_for_ingress(ingress)})}


def snapshot_delta(snapshot, remote):
	"""The sections whose hash the box does not already hold. A bucket the box hashes but the
	snapshot no longer has is sent explicitly EMPTY, so the agent prunes its members instead of
	holding them forever."""
	delta = {}
	for section, content in snapshot.items():
		if "buckets" in content:
			changed = {
				label: bucket
				for label, bucket in content["buckets"].items()
				if remote.get(f"{section}:{label}") != bucket["hash"]
			}
			held = {k.split(":", 1)[1] for k in remote if k.startswith(f"{section}:")}
			for label in held - set(content["buckets"]):
				changed[label] = {"records": []}
			if changed:
				delta[section] = {"buckets": changed}
		elif remote.get(section) != content["hash"]:
			delta[section] = content
	return delta


def section_label(section, content):
	"""`keys[3]` for a bucketed section with three buckets in play, the bare name otherwise."""
	buckets = content.get("buckets")
	return f"{section}[{len(buckets)}]" if buckets is not None else section


def describe(delta, response):
	"""Which sections went (bucket counts in brackets) and how many records the agent wrote."""
	counts = (response or {}).get("counts") or {}
	parts = []
	for section in ("groups", "users", "keys", "routes"):
		if section not in delta:
			continue
		label = section_label(section, delta[section])
		if section in counts:
			label = f"{label}:{counts[section]}"
		parts.append(label)
	return " ".join(parts)
