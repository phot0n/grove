# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Project Grove state (keys + routing table) into each Proxy Server's local
Redis via the gateway agent's token-gated admin API (§6). Grove is the source
of truth. The gateway `/keys` and `/routes` endpoints are UPSERTs (they never
prune), so we can push either everything or just a delta.

Two paths:
  * full_sync(proxies)  — push the COMPLETE key set + routing table. Manual
    (buttons), proxy activation, and provisioning use this.
  * sync_dirty()        — background job (cron): push ONLY the keys/deployments
    flagged `dirty` since the last sync, then clear their flag. Failures stay
    dirty and are retried on the next tick.

Both log one Gateway Sync doc per run with a per-proxy
child row, and both serialize against each other via a MariaDB advisory lock so
a slow run can't land a stale write after a newer one."""

import time

import requests

import frappe

TIMEOUT = 10
_ALL = object()  # sentinel: push the complete set (vs. a subset list, vs. None = skip)


def _conn(proxy_name):
	p = frappe.get_doc("Proxy Server", proxy_name)
	if not p.admin_url:
		frappe.throw(f"Proxy Server {proxy_name} has no admin_url")
	return p, p.admin_url.rstrip("/"), (p.get_password("admin_token") or "")


def _post(admin_url, token, path, payload):
	r = requests.post(
		f"{admin_url}/{path}",
		json=payload,
		headers={"X-Grove-Admin-Token": token, "Content-Type": "application/json"},
		timeout=TIMEOUT,
	)
	r.raise_for_status()
	return r.json()


def _effective_keys(proxy):
	"""All API Keys projected for the gateway: identity + status + allowed_models.
	The only rate limit is the monthly token budget, surfaced via status
	(rate_limited); no per-request rpm/tpm/concurrency knobs anymore."""
	rows = frappe.get_all(
		"API Key",
		fields=["name", "key_hash", "user", "status", "rate_limited", "allowed_models"],
	)
	keys = []
	for k in rows:
		if not k.key_hash:
			continue
		# The gateway keeps no rate counters — the only limit is the monthly token
		# budget, which the control plane flags as rate_limited, surfaced here as a
		# distinct status (→ 429). Revoked still wins; the budget only gates active
		# keys.
		status = k.status or "active"
		if status == "active" and k.rate_limited:
			status = "rate_limited"
		keys.append({
			"key_hash": k.key_hash,
			"prefix": k.name,  # doc name (random hash) = usage attribution id
			"user": k.user or "",
			"status": status,
			"allowed_models": (k.allowed_models or "").replace("\n", ",").replace(" ", ""),
		})
	return keys


def _routes_for_proxy(proxy_name):
	"""deploy:<model> table (global — same for every proxy since deployments are no
	longer proxy-scoped): every model with a Model Deployment maps to its Active
	engines (empty list → the agent deletes the key → 503). proxy_name is unused,
	kept for the sync_routes call signature."""
	deps = frappe.get_all(
		"Model Deployment",
		fields=["name", "model", "engine_url", "status"],
	)
	routes = {}
	for d in deps:
		routes.setdefault(d.model, [])
		if d.status == "Active":
			internal_key = frappe.get_doc("Model Deployment", d.name).get_password("internal_api_key") or ""
			routes[d.model].append({
				"engine_url": d.engine_url,
				"internal_key": internal_key,
				"healthy": True,
			})
	return routes


def push_keys(proxy, keys=_ALL):
	"""Upsert keys to one proxy. keys=_ALL → the whole set; a list of API Key
	names → just those (the agent HSETs each; other keys are left untouched)."""
	p, admin_url, token = _conn(proxy)
	eff = _effective_keys(p)
	if keys is not _ALL:
		wanted = set(keys)
		eff = [k for k in eff if k["prefix"] in wanted]
	return _post(admin_url, token, "keys", {"keys": eff})


def sync_routes(proxy, models=_ALL):
	"""Replace the routing table for models on one proxy. models=_ALL → every
	model deployed here; a set/list → just those (empty engine set → the agent
	DELs deploy:<model> → 503)."""
	_p, admin_url, token = _conn(proxy)
	routes = _routes_for_proxy(proxy)
	if models is not _ALL:
		routes = {m: routes.get(m, []) for m in models}
	return _post(admin_url, token, "routes", {"routes": routes})


# --- Sync runs -------------------------------------------------------------

def full_sync(proxies=None, trigger="Manual"):
	"""Push the COMPLETE key set + routing table to each target proxy.
	proxies=None → all Active. Used by buttons, proxy activation, provisioning."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=60):  # forced → queue behind an in-flight run
		return None
	try:
		all_active = _active_proxies()
		active = proxies or all_active
		if not active:
			return None

		keys_snap = _snapshot_dirty("API Key")

		ok = 0
		for proxy in active:
			res = _push_and_classify(proxy, _ALL, _ALL)
			doc.append("results", {"proxy": proxy, **res})
			ok += res["success"]
		_finalize(doc, active, ok)

		# A full run that reached every Active proxy has projected all current
		# keys, so the dirty keys it covered can clear. (Subset runs clear
		# nothing global; routes are not dirty-gated.)
		if doc.status == "Success" and set(active) >= set(all_active):
			_clear_unchanged("API Key", keys_snap)

		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def sync_dirty(trigger="Scheduled"):
	"""Background job (cron): push dirty keys + the full route table for every
	deployment to the relevant Active proxies, then clear each key that landed
	(key failures stay dirty → retried next tick). Routes are NOT dirty-gated —
	they're pushed every run (idempotent). Skips (no doc) when there's nothing to
	push (no dirty keys and no deployments)."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=0):  # scheduled → skip if a run is in flight
		return None
	try:
		dirty_keys = frappe.get_all("API Key", filters={"dirty": 1}, fields=["name", "modified"])
		has_deps = bool(frappe.db.count("Model Deployment"))
		if not dirty_keys and not has_deps:
			return None

		all_active = set(_active_proxies())
		if not all_active:
			return None  # work exists but no Active proxy can receive it yet

		# Routes are global (no per-deployment proxy) → every Active proxy gets the
		# full route table each tick (idempotent, not dirty-gated). Keys are global too.
		route_targets = all_active if has_deps else set()
		key_targets = all_active if dirty_keys else set()
		targets = key_targets | route_targets

		key_names = [k.name for k in dirty_keys]
		ok_by_proxy = {}
		total_ok = 0
		for proxy in sorted(targets):
			keys_arg = key_names if proxy in key_targets else None
			models_arg = _ALL if proxy in route_targets else None
			res = _push_and_classify(proxy, keys_arg, models_arg)
			doc.append("results", {"proxy": proxy, **res})
			ok_by_proxy[proxy] = bool(res["success"])
			total_ok += res["success"]
		_finalize(doc, list(targets), total_ok)

		# Keys clear only once every key-target proxy accepted them.
		if dirty_keys and all(ok_by_proxy.get(p) for p in key_targets):
			_clear_unchanged("API Key", dirty_keys)

		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def _push_and_classify(proxy, keys, models):
	"""Push the requested keys and/or routes to one proxy (None = skip that
	push); report reachability and success separately so the log distinguishes
	'proxy down' from 'up but rejected'."""
	start = time.monotonic()
	reachable, success, http_status, error = 1, 0, 0, None
	detail = []
	try:
		if keys is not None:
			r = push_keys(proxy, keys)
			detail.append(f"keys:{r.get('count', '?')}")
		if models is not None:
			r = sync_routes(proxy, models)
			detail.append(f"routes:{r.get('models', '?')}")
		success = 1
	except (requests.ConnectionError, requests.Timeout) as e:
		reachable, error = 0, f"{type(e).__name__}: {e}"[:2000]
	except requests.HTTPError as e:
		http_status = e.response.status_code if e.response is not None else 0
		error = f"HTTP {http_status}: {e}"[:2000]
	except Exception as e:  # config error (e.g. no admin_url), etc.
		error = f"{type(e).__name__}: {e}"[:2000]
	return {
		"reachable": reachable,
		"success": success,
		"http_status": http_status,
		"error": error,
		"duration_ms": int((time.monotonic() - start) * 1000),
		"detail": " ".join(detail),
	}


# --- helpers ---------------------------------------------------------------

def _active_proxies():
	return frappe.get_all("Proxy Server", filters={"status": "Active"}, pluck="name")


def _new_run(sync_type, trigger):
	doc = frappe.new_doc("Gateway Sync")
	doc.run_at = frappe.utils.now_datetime()
	doc.sync_type = sync_type
	doc.trigger = trigger
	return doc


def _finalize(doc, proxies, ok):
	doc.proxies_total = len(proxies)
	doc.proxies_ok = ok
	doc.status = "Success" if ok == len(proxies) else ("Failed" if ok == 0 else "Partial")
	doc.insert(ignore_permissions=True)


def _snapshot_dirty(doctype):
	return frappe.get_all(doctype, filters={"dirty": 1}, fields=["name", "modified"])


def _clear_unchanged(doctype, rows, only=None):
	"""Clear `dirty` for snapshot rows not modified since the snapshot — an edit
	during the run bumps `modified`, so that item stays dirty for the next run
	instead of being wrongly marked synced."""
	for r in rows:
		if only is not None and r.name not in only:
			continue
		if frappe.db.get_value(doctype, r.name, "modified") == r.modified:
			frappe.db.set_value(doctype, r.name, "dirty", 0, update_modified=False)
