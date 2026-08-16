# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Pull token usage from each Gateway Server into monthly Usage Records (§6 job 3).

The gateway no longer tracks the month — it just accumulates per-key deltas in
`usage:<prefix>`. On pull we: (1) GET /usage, which atomically reads-and-deletes
(HGETALL + DEL in one Lua call) each live counter and returns it; (2) stamp the
month from OUR clock (UTC, matching grove.api.usage) and ADD the delta into the
(key, month) record. No second round trip — the counter is already gone.

**1-shot / no retry**: the drain deletes the counter as it returns it, so a
failed pull is NOT retried. A crash between the GET response and the insert
commit loses that cycle's delta. Net: never double-count, rare bounded loss on
failure. Requests metered mid-pull are safe (atomic drain → they land either
fully in the pulled snapshot or on the fresh live key, never split).

Each run is logged as a **Agent Sync** doc (sync_type=Usage) with a per-proxy
child row, same as the keys/routes sync, and serialized by that doc's own lock so
two overlapping pulls can't drain the same counters twice."""

import time

import requests

import frappe

from grove.agent_sync import _active_proxies, _finalize, _new_run
from grove.grove.doctype.grove_user.grove_user import monthly_budget, set_rate_limited
from grove.grove.doctype.usage_record.usage_record import billable_tokens, current_month

TIMEOUT = 15
_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "request_count")


def pull_all():
	"""Scheduled: pull + drain usage from every Active proxy, logged as one
	Agent Sync doc. Skips if another pull is in flight."""
	doc = _new_run("Usage", "Scheduled")
	if not doc.acquire_lock(wait=0):  # scheduled → skip if a pull is in flight
		return None
	try:
		active = _active_proxies()
		if not active:
			return None
		ok = 0
		for proxy in active:
			res = _pull_and_classify(proxy)
			doc.append("results", {"server_type": "Gateway Server", "server": proxy, **res})
			ok += res["success"]
		_finalize(doc, len(active), ok)
		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def reactivate_rate_limited():
	"""Daily job: clear rate_limited for users whose CURRENT-month usage is
	back under their budget — i.e. the month rolled over (no Usage Records yet) or the
	budget was raised. A user still over budget for the still-active current month stays
	blocked (the monthly cap is HARD — no daily burst). Runs independently of traffic so
	a blocked user still gets un-limited at month rollover (they otherwise see no usage to
	re-fire the on_update). The budget is per-user and shared across their keys, so clearing it
	unblocks all of them at once. Returns the count of users reactivated."""
	month = current_month()
	users = frappe.get_all("Grove User", filters={"rate_limited": 1}, pluck="name")
	cleared = 0
	for user in users:
		limit = monthly_budget(user)
		if limit and billable_tokens(user, month) >= limit:
			continue
		cleared += set_rate_limited(user, 0)
	if cleared:
		frappe.db.commit()
	return cleared


def _pull_and_classify(proxy_name):
	"""Pull + drain one proxy; report reachability + success separately (matches
	the keys/routes sync rows) so the log distinguishes 'down' from 'rejected'."""
	start = time.monotonic()
	reachable, success, http_status, error, detail, had_data = 1, 0, 0, None, "", 0
	try:
		pulled = _pull_proxy(proxy_name)
		had_data = 1 if pulled else 0
		detail = f"pulled:{pulled}"
		success = 1
	except (requests.ConnectionError, requests.Timeout) as e:
		reachable, error = 0, f"{type(e).__name__}: {e}"[:2000]
	except requests.HTTPError as e:
		http_status = e.response.status_code if e.response is not None else 0
		error = f"HTTP {http_status}: {e}"[:2000]
	except Exception as e:
		error = f"{type(e).__name__}: {e}"[:2000]
	return {
		"reachable": reachable,
		"success": success,
		"had_data": had_data,
		"http_status": http_status,
		"error": error,
		"duration_ms": int((time.monotonic() - start) * 1000),
		"detail": detail,
	}


def _pull_proxy(proxy_name):
	"""GET /usage (atomically reads-and-deletes each live counter), record the
	deltas under this month, and commit. Returns the count of keys pulled."""
	p = frappe.get_doc("Gateway Server", proxy_name)
	admin_url = (p.admin_url or "").rstrip("/")
	token = p.get_password("admin_token")

	r = requests.get(
		f"{admin_url}/usage", headers={"X-Grove-Admin-Token": token}, timeout=TIMEOUT
	)
	r.raise_for_status()
	usages = r.json().get("usages", {})
	if not usages:
		return 0

	month = current_month()
	for prefix, h in usages.items():
		amounts = {f: int(h.get(f, 0) or 0) for f in _FIELDS}
		per_model = _per_model(h)
		# Record only for keys registered here; unregistered (e.g. manual test
		# keys) are dropped — the gateway already deleted the counter on read.
		if any(amounts.values()) and (user := frappe.db.get_value("Grove API Key", prefix, "user")):
			_add_delta(proxy_name, prefix, user, month, amounts, per_model)

	frappe.db.commit()
	return len(usages)


def _per_model(h):
	"""Split the per-model breakdown out of a drained usage hash. The gateway writes
	per-model counters as `m:<metric>:<model>` fields alongside the flat aggregate, so
	one atomic drain carries both. → {model: {metric: delta}}."""
	per_model = {}
	for k, v in h.items():
		if not k.startswith("m:"):
			continue
		metric, _, model = k[2:].partition(":")  # model may contain ':' — keep the rest
		if metric in _FIELDS and model:
			per_model.setdefault(model, {})[metric] = int(v or 0)
	return per_model


def _add_delta(proxy_name, prefix, user, month, amounts, per_model=None):
	"""ADD a pulled delta to the (api_key, month) Usage Record's per-gateway row,
	then roll the doc totals up from the rows (zero-loss aggregate). per_model deltas
	accumulate into per-model rows (summed across gateways, like the top-level totals)."""
	if name := frappe.db.exists("Usage Record", {"month": month, "api_key": prefix}):
		doc = frappe.get_doc("Usage Record", name)
	else:
		doc = frappe.new_doc("Usage Record")
		doc.api_key = prefix
		doc.month = month
	doc.user = user

	row = next((r for r in doc.gateway_usage if r.gateway_server == proxy_name), None)
	if not row:
		row = doc.append("gateway_usage", {"gateway_server": proxy_name})
	for f in _FIELDS:
		row.set(f, (row.get(f) or 0) + amounts[f])
	row.last_pulled = frappe.utils.now()

	# Top-level totals = sum of per-gateway rows.
	for f in _FIELDS:
		doc.set(f, sum(gr.get(f) or 0 for gr in doc.gateway_usage))

	# Per-model breakdown: add-accumulate (drain is delete-on-read, so deltas). Skip
	# models no longer in the Model doctype so a stale name can't fail the whole pull;
	# their tokens still land in the flat totals above.
	for model, deltas in (per_model or {}).items():
		if not frappe.db.exists("Model", model):
			continue
		mrow = next((r for r in doc.model_usage if r.model == model), None)
		if not mrow:
			mrow = doc.append("model_usage", {"model": model})
		for f in _FIELDS:
			mrow.set(f, (mrow.get(f) or 0) + int(deltas.get(f, 0)))

	doc.save(ignore_permissions=True)
