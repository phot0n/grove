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

from grove.access import effective_models
from grove.grove.doctype.grove_user.grove_user import is_rate_limited

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
	"""All API Keys projected for the gateway: identity + status + the flat set of models
	the key may call. The only rate limit is the monthly token budget, surfaced via status
	(rate_limited); no per-request rpm/tpm/concurrency knobs anymore."""
	rows = frappe.get_all(
		"Grove API Key",
		fields=["name", "key_hash", "user", "status"],
	)
	# Access is a property of the user, not the key — resolve each user once even when
	# they hold several keys.
	per_user = {}
	keys = []
	for k in rows:
		if not k.key_hash:
			continue
		# The gateway keeps no rate counters — the only limit is the monthly token
		# budget, which the control plane flags on the USER, surfaced here as a distinct
		# status (→ 429). Revoked still wins; the budget only gates active keys. Reading
		# it off the user is what stops a blocked user minting a fresh, unblocked key.
		if k.user not in per_user:
			per_user[k.user] = (
				frappe.db.get_value("Grove User", k.user, "user") or "",
				sorted(effective_models(k.user)),
				is_rate_limited(k.user),
			)
		email, models, limited = per_user[k.user]
		status = k.status or "active"
		if status == "active" and limited:
			status = "rate_limited"
		keys.append({
			"key_hash": k.key_hash,
			"prefix": k.name,  # doc name (random hash) = usage attribution id
			"user": email,  # email, not the Grove User name — this one is for humans reading Redis
			"status": status,
			"models": ",".join(models),
		})
	return keys


def _routes_for_proxy(proxy_name):
	"""deploy:<model> table (global — same for every proxy since deployments are no
	longer proxy-scoped): every model maps to its Active engines (empty list → the
	agent deletes the key → 503). Seeded with EVERY Model so an unpublished one
	(last MD deleted, Pod no longer Running) is explicitly sent as empty and pruned
	from Redis — otherwise its stale key survives and keeps showing in /v1/models.
	proxy_name is unused, kept for the sync_routes call signature."""
	deps = frappe.get_all(
		"Model Deployment",
		fields=["name", "model", "engine_url", "status", "inference_server"],
	)
	routes = {m: [] for m in frappe.get_all("Model", pluck="name")}
	for d in deps:
		routes.setdefault(d.model, [])
		if d.status == "Active":
			internal_key = frappe.get_doc("Model Deployment", d.name).get_password("internal_api_key") or ""
			routes[d.model].append({
				"engine_url": d.engine_url,
				"internal_key": internal_key,
				"healthy": True,
				"server": d.inference_server or d.name,  # request-id target part
			})

	# Standalone serving Pods (a vLLM image serving the Model directly — no Model Deployment)
	# register the same way: deploy:<model> → engine. Only Running pods with a derived
	# engine_url contribute; others are dropped so the agent 503s instead of routing to a
	# dead endpoint. A model served by both an MD and a Pod gets both engines (load-balanced).
	for p in frappe.get_all("Pod", filters={"status": "Running"}, fields=["name", "model", "engine_url"]):
		if not p.engine_url:
			continue
		routes.setdefault(p.model, [])
		internal_key = frappe.get_doc("Pod", p.name).get_password("api_key") or ""
		routes[p.model].append({
			"engine_url": p.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"server": p.name,  # request-id target part
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

		keys_snap = _snapshot_dirty("Grove API Key")

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
			_clear_unchanged("Grove API Key", keys_snap)

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
		dirty_keys = frappe.get_all("Grove API Key", filters={"dirty": 1}, fields=["name", "modified"])
		# Routes come from Model Deployments AND standalone serving Pods (Running).
		has_deps = bool(frappe.db.count("Model Deployment")) or bool(
			frappe.db.count("Pod", {"status": "Running"})
		)
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
			_clear_unchanged("Grove API Key", dirty_keys)

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
