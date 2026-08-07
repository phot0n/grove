# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Project Grove state (groups + keys + routing table) into each Proxy Server's
local Redis via the gateway agent's token-gated admin API (§6). Grove is the
source of truth. The gateway `/groups`, `/keys` and `/routes` endpoints are
UPSERTs (they never prune), so we can push either everything or just a delta.

Access is pushed in two pieces, deliberately. A group carries the model grant and
the priority for everyone in it (group:<name>); a key carries only which group it
belongs to plus that user's own allow/deny. So a group edit is ONE record, not one
per key its members hold, and the gateway resolves the three at request time.

Two paths:
  * full_sync(proxies)  — push the COMPLETE group + key set + routing table. Manual
    (buttons), proxy activation, and provisioning use this.
  * sync_dirty()        — background job (cron): push ONLY the groups/keys
    flagged `dirty` since the last sync, then clear their flag. Failures stay
    dirty and are retried on the next tick.

Both log one Gateway Sync doc per run with a per-proxy
child row, and both serialize against each other via a MariaDB advisory lock so
a slow run can't land a stale write after a newer one."""

import time

import requests

import frappe

from grove.access import key_access, vllm_priority
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


def _effective_groups():
	"""Every Grove User Group projected for the gateway: what it grants and how far ahead of
	the baseline its members are served. One record per group however many keys point at it —
	the reason the group is not flattened onto each key."""
	rows = frappe.get_all(
		"Grove Model Row",
		filters={"parenttype": "Grove User Group"},
		fields=["parent", "model"],
	)
	models = {}
	for r in rows:
		models.setdefault(r.parent, []).append(r.model)
	return [
		{
			"name": g.name,
			# Already in vLLM's convention — the gateway just stamps it.
			"priority": vllm_priority(g.priority),
			"models": ",".join(sorted(models.get(g.name, []))),
		}
		for g in frappe.get_all("Grove User Group", fields=["name", "priority"])
	]


def _effective_keys(proxy):
	"""All API Keys projected for the gateway: identity + status + which group the key inherits
	its access from + this user's own allow/deny on top of it. The only rate limit is the
	monthly token budget, surfaced via status (rate_limited); no per-request rpm/tpm/
	concurrency knobs anymore."""
	rows = frappe.get_all(
		"Grove API Key",
		fields=["name", "key_hash", "user", "status"],
	)
	# Access is a property of the user, not the key — read each user once even when
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
				key_access(k.user),
				is_rate_limited(k.user),
			)
		email, (group, allow, deny), limited = per_user[k.user]
		status = k.status or "active"
		if status == "active" and limited:
			status = "rate_limited"
		keys.append({
			"key_hash": k.key_hash,
			"prefix": k.name,  # doc name (random hash) = usage attribution id
			"user": email,  # email, not the Grove User name — this one is for humans reading Redis
			"status": status,
			"group": group,  # the gateway reads group:<name> for the grant and the priority
			"allow": ",".join(allow),
			"deny": ",".join(deny),
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
				# Which placement was chosen (request-id target part, access-log deployment=).
				# A box can serve the same model twice, so the server alone cannot name an engine.
				"deployment": d.name,
				"server": d.inference_server or d.name,  # which box it is on
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
			# A pod IS its own placement — it carries no separate deployment doc, so both
			# fields are the pod. Kept explicit so consumers never special-case a pod route.
			"deployment": p.name,
			"server": p.name,
		})
	return routes


def push_groups(proxy, groups=_ALL):
	"""Upsert user groups to one proxy. groups=_ALL → every group; a list of Grove User Group
	names → just those (the agent HSETs each; other groups are left untouched)."""
	_p, admin_url, token = _conn(proxy)
	eff = _effective_groups()
	if groups is not _ALL:
		wanted = set(groups)
		eff = [g for g in eff if g["name"] in wanted]
	return _post(admin_url, token, "groups", {"groups": eff})


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
	"""Push the COMPLETE group + key set + routing table to each target proxy.
	proxies=None → all Active. Used by buttons, proxy activation, provisioning."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=60):  # forced → queue behind an in-flight run
		return None
	try:
		all_active = _active_proxies()
		active = proxies or all_active
		if not active:
			return None

		snaps = {d: _snapshot_dirty(d) for d in ("Grove User Group", "Grove API Key")}

		ok = 0
		for proxy in active:
			res = _push_and_classify(proxy, _ALL, _ALL, _ALL)
			doc.append("results", {"proxy": proxy, **res})
			ok += res["success"]
		_finalize(doc, active, ok)

		# A full run that reached every Active proxy has projected all current
		# groups and keys, so the dirty ones it covered can clear. (Subset runs
		# clear nothing global; routes are not dirty-gated.)
		if doc.status == "Success" and set(active) >= set(all_active):
			for doctype, snap in snaps.items():
				_clear_unchanged(doctype, snap)

		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def sync_dirty(trigger="Scheduled"):
	"""Background job (cron): push dirty groups + dirty keys + the full route table for
	every deployment to the relevant Active proxies, then clear each item that landed
	(failures stay dirty → retried next tick). Routes are NOT dirty-gated — they're pushed
	every run (idempotent). Skips (no doc) when there's nothing to push (nothing dirty and
	no deployments).

	A group edit dirties ONE row here, whatever it grants and to however many keys — the
	fan-out it used to cause is exactly what moving the group into its own record removed."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=0):  # scheduled → skip if a run is in flight
		return None
	try:
		dirty_groups = _snapshot_dirty("Grove User Group")
		dirty_keys = _snapshot_dirty("Grove API Key")
		# Routes come from Model Deployments AND standalone serving Pods (Running).
		has_deps = bool(frappe.db.count("Model Deployment")) or bool(
			frappe.db.count("Pod", {"status": "Running"})
		)
		if not dirty_groups and not dirty_keys and not has_deps:
			return None

		all_active = set(_active_proxies())
		if not all_active:
			return None  # work exists but no Active proxy can receive it yet

		# Routes are global (no per-deployment proxy) → every Active proxy gets the
		# full route table each tick (idempotent, not dirty-gated). Groups and keys are
		# global too.
		route_targets = all_active if has_deps else set()
		group_targets = all_active if dirty_groups else set()
		key_targets = all_active if dirty_keys else set()
		targets = group_targets | key_targets | route_targets

		group_names = [g.name for g in dirty_groups]
		key_names = [k.name for k in dirty_keys]
		ok_by_proxy = {}
		total_ok = 0
		for proxy in sorted(targets):
			groups_arg = group_names if proxy in group_targets else None
			keys_arg = key_names if proxy in key_targets else None
			models_arg = _ALL if proxy in route_targets else None
			res = _push_and_classify(proxy, groups_arg, keys_arg, models_arg)
			doc.append("results", {"proxy": proxy, **res})
			ok_by_proxy[proxy] = bool(res["success"])
			total_ok += res["success"]
		_finalize(doc, list(targets), total_ok)

		# Each doctype clears only once every proxy it targeted accepted the push.
		for doctype, rows, pushed_to in (
			("Grove User Group", dirty_groups, group_targets),
			("Grove API Key", dirty_keys, key_targets),
		):
			if rows and all(ok_by_proxy.get(p) for p in pushed_to):
				_clear_unchanged(doctype, rows)

		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def _push_and_classify(proxy, groups, keys, models):
	"""Push the requested groups and/or keys and/or routes to one proxy (None = skip that
	push); report reachability and success separately so the log distinguishes
	'proxy down' from 'up but rejected'.

	Groups go first: a key names its group, and a key that landed ahead of a brand-new group
	would resolve against a record that is not there yet and 403 until the next tick."""
	start = time.monotonic()
	reachable, success, http_status, error = 1, 0, 0, None
	detail = []
	try:
		if groups is not None:
			r = push_groups(proxy, groups)
			detail.append(f"groups:{r.get('count', '?')}")
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
