# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Project Grove state (groups + users + keys + routing table) into each Gateway Server's
local Redis via the gateway agent's token-gated admin API (§6). Grove is the
source of truth. The gateway `/groups`, `/users`, `/keys` and `/routes` endpoints are
UPSERTs (they never prune), so we can push either everything or just a delta.

Access is pushed in three pieces, deliberately, one per thing that can change on its own. A group
carries the model grant and the priority for everyone in it (group:<name>); a user carries which
group they belong to, their own allow/deny, and whether they are over budget (user:<name>); a key
carries only whose it is and whether it has been revoked. So a group edit is ONE record however
many members, a budget flip is ONE record however many keys, and the gateway resolves the three at
request time.

Two paths:
  * full_sync(proxies)  — push the COMPLETE group + user + key set + routing table. Manual
    (buttons), proxy activation, and provisioning use this.
  * sync_dirty()        — background job (cron): push ONLY the groups/users/keys
    flagged `dirty` since the last sync, then clear their flag. Failures stay
    dirty and are retried on the next tick.

Both log one Gateway Sync doc per run with a per-proxy
child row, and both serialize against each other via a MariaDB advisory lock so
a slow run can't land a stale write after a newer one."""

import time

import requests

import frappe

from grove.access import model_rows, vllm_priority
from grove.net import private_url
from grove.serve_command import DEFAULT_MAX_NUM_SEQS

TIMEOUT = 10
_ALL = object()  # sentinel: push the complete set (vs. a subset list, vs. None = skip)

# Pointer order — a key names its user, a user names their group — and _push_and_classify takes
# its arguments in the same order, so a record never lands ahead of the one it resolves against.
_DIRTY_DOCTYPES = ("Grove User Group", "Grove User", "Grove API Key")


def _conn(name, doctype="Gateway Server"):
	"""The admin API of one box, whichever plane it is on. Both kinds derive admin_url the same
	way and both gate /admin on the same token — what differs is which pushes they are sent."""
	p = frappe.get_doc(doctype, name)
	if not p.admin_url:
		frappe.throw(f"{doctype} {name} has no admin_url")
	return p, p.admin_url.rstrip("/"), (p.get_password("admin_token") or "")


def _post(admin_url, token, path, payload, method="POST"):
	r = requests.request(
		method,
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
	granted = model_rows("Grove User Group")
	return [
		{
			"name": g.name,
			# Already in vLLM's convention — the gateway just stamps it.
			"priority": vllm_priority(g.priority),
			"models": ",".join(granted.get(g.name, {}).get("models", [])),
		}
		for g in frappe.get_all("Grove User Group", fields=["name", "priority"])
	]


def _public_catalog():
	"""Every model named by a group flagged Show in Public Catalogue, as one comma list.

	Pooled here rather than left to the gateway to assemble: it is the only way the anonymous
	endpoint stays a single read, and a group deleted here simply stops appearing — the gateway
	holds no per-group state it would have to garbage-collect.

	Names only. This grants nothing: the inference path still resolves a key against its group,
	and a model on this list is refused without one."""
	public = frappe.get_all("Grove User Group", filters={"public_catalog": 1}, pluck="name")
	if not public:
		return ""
	rows = frappe.get_all(
		"Grove Model Row",
		filters={"parenttype": "Grove User Group", "parent": ("in", public)},
		pluck="model",
	)
	return ",".join(sorted(set(rows)))


def _effective_users(users=None):
	"""Every Grove User projected for the gateway: their group, their own allow/deny, and whether
	they are over their monthly budget. One record per person however many keys they hold — the
	reason none of this is flattened onto the keys.

	The gateway keeps no rate counters. The only limit is the monthly token budget, which the
	control plane flags here and the gateway honours as a 429. Holding it on the person is what
	stops a blocked user minting a fresh, unblocked key."""
	filters = {"name": ("in", list(users))} if users is not None else {}
	deltas = model_rows("Grove User", users)
	return [
		{
			"name": u.name,
			"email": u.user or "",  # for humans reading Redis; no decision reads it
			# The gateway reads group:<name> for the grant and the priority.
			"group": u.user_group or "",
			"allow": ",".join(deltas.get(u.name, {}).get("allow", [])),
			"deny": ",".join(deltas.get(u.name, {}).get("deny", [])),
			"limited": bool(u.rate_limited),
		}
		for u in frappe.get_all(
			"Grove User", filters=filters, fields=["name", "user", "user_group", "rate_limited"]
		)
	]


def _effective_keys():
	"""Every LIVE API Key projected for the gateway. A key is a pointer to whoever holds it and
	nothing else — what they may call and whether they are over budget belongs to the person and
	is pushed as their user record.

	Revoked keys are not projected at all. The row stays in Grove as the record of a credential
	that existed, but the gateways are told to forget it (Gateway Deletion) rather than to hold a
	tombstone of their own: an unknown key already 401s, so there is nothing a dead record adds."""
	return [
		{
			"key_hash": k.key_hash,
			"prefix": k.name,  # doc name (random hash) = usage attribution id
			"user": k.user,  # Grove User doc name — the pointer to user:<name>
			"status": k.status or "active",
		}
		for k in frappe.get_all(
			"Grove API Key", filters={"status": "active"}, fields=["name", "key_hash", "user", "status"]
		)
		if k.key_hash
	]


def _routes_for_proxy(proxy_name):
	"""deploy:<model> table (global — same for every proxy since deployments are no
	longer proxy-scoped): every model maps to its Active engines (empty list → the
	agent deletes the key → 503). Seeded with EVERY Model so an unpublished one
	(last MD deleted, Pod no longer Running) is explicitly sent as empty and pruned
	from Redis — otherwise its stale key survives and keeps showing in /v1/models.
	proxy_name is unused, kept for the sync_routes call signature."""
	deps = frappe.get_all(
		"Model Deployment",
		fields=["name", "model", "engine_url", "status", "inference_server", "max_num_seqs"],
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
				# --max-num-seqs is what this engine runs at once; past it vLLM queues. The
				# gateway holds admissions to the same number so the queue forms across
				# replicas instead of inside one. Blank resolves to the same default the serve
				# command uses — the two have to agree or the cap is not the engine's.
				"capacity": int(d.max_num_seqs or DEFAULT_MAX_NUM_SEQS),
				# Which placement was chosen (request-id target part, access-log deployment=).
				# A box can serve the same model twice, so the server alone cannot name an engine.
				"deployment": d.name,
				"server": d.inference_server or d.name,  # which box it is on
			})

	# Standalone serving Pods (a vLLM image serving the Model directly — no Model Deployment)
	# register the same way: deploy:<model> → engine. Only Running pods with a derived
	# engine_url contribute; others are dropped so the agent 503s instead of routing to a
	# dead endpoint. A model served by both an MD and a Pod gets both engines (load-balanced).
	pods = frappe.get_all(
		"Pod", filters={"status": "Running"}, fields=["name", "model", "engine_url", "max_num_seqs"]
	)
	for p in pods:
		# A pod with no Model serves something the gateway has no route key for (an ASR
		# container, say) — it is reached directly, not through deploy:<model>.
		if not (p.model and p.engine_url):
			continue
		routes.setdefault(p.model, [])
		internal_key = frappe.get_doc("Pod", p.name).get_password("api_key") or ""
		routes[p.model].append({
			"engine_url": p.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": int(p.max_num_seqs or DEFAULT_MAX_NUM_SEQS),
			# A pod IS its own placement — it carries no separate deployment doc, so both
			# fields are the pod. Kept explicit so consumers never special-case a pod route.
			"deployment": p.name,
			"server": p.name,
		})
	return routes


def _owned_boxes(ingress_name):
	"""{Inference Server name: private ip} for the boxes that name this ingress.

	Ownership is explicit rather than derived from the Network, and that is what keeps the
	capacity gate honest. `inflight:<engine>` lives in each box's own Redis, so a replica counted
	by two ingresses is admitted to twice its --max-num-seqs and vLLM starts queueing — silently.
	One owner per replica means one authoritative count.

	A box with no private address is left out, not dialled publicly. Fail closed: the model reads
	unavailable behind this ingress and someone syncs the Machine, rather than customer traffic
	quietly crossing the internet to a box that was supposed to be private."""
	servers = frappe.get_all("Inference Server", filters={"ingress": ingress_name}, fields=["name", "machine"])
	machines = {
		machine["name"]: machine
		for machine in frappe.get_all(
			"Machine",
			filters={"name": ("in", [s["machine"] for s in servers if s["machine"]])},
			fields=["name", "private_ip"],
		)
	} if servers else {}
	return {
		server["name"]: machines[server["machine"]]["private_ip"]
		for server in servers
		if server["machine"] in machines and machines[server["machine"]]["private_ip"]
	}


def _replicas_for_ingress(ingress_name):
	"""deploy:<model> for ONE ingress: every Active replica it owns, dialled privately.

	The same shape the gateway's table has, so the agent's /admin/routes handler is reused whole —
	what narrows is the scope. An ingress is told about its own boxes and no others, so a pod
	restarting in another region is not an event it ever sees. That is the point of the split:
	replica topology never leaves its own Network.

	Seeded with EVERY Model, so a model with nothing here is sent as an empty list, the agent DELs
	the key, and /pick answers 503 no-replica rather than the model quietly resolving somewhere it
	should not.

	Pods are absent by construction — they have no Machine and so no ingress, and stay on the
	gateway's direct route kind."""
	owned = _owned_boxes(ingress_name)
	routes = {model: [] for model in frappe.get_all("Model", pluck="name")}
	if not owned:
		return routes

	deployments = frappe.get_all(
		"Model Deployment",
		filters={"status": "Active", "inference_server": ("in", list(owned))},
		fields=["name", "model", "engine_url", "inference_server", "max_num_seqs"],
	)
	for deployment in deployments:
		engine_url = private_url(deployment.engine_url, owned[deployment.inference_server])
		if not engine_url:
			continue
		routes.setdefault(deployment.model, [])
		internal_key = frappe.get_doc("Model Deployment", deployment.name).get_password("internal_api_key") or ""
		routes[deployment.model].append({
			"engine_url": engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": int(deployment.max_num_seqs or DEFAULT_MAX_NUM_SEQS),
			"deployment": deployment.name,
			"server": deployment.inference_server,
		})
	return routes


def sync_replicas(ingress):
	"""Replace one ingress's replica table. The only push an ingress ever takes — there is no
	keys, users, groups or usage endpoint on that plane to send anything else to."""
	_doc, admin_url, token = _conn(ingress, "Ingress Server")
	return _post(admin_url, token, "routes", {"routes": _replicas_for_ingress(ingress)})


def push_groups(proxy, groups=_ALL):
	"""Upsert user groups to one proxy. groups=_ALL → every group; a list of Grove User Group
	names → just those (the agent HSETs each; other groups are left untouched).

	The public catalogue rides every groups push, complete, even a one-group one: it is a pooled
	list, so a subset push that carried only its own share would erase everybody else's."""
	_p, admin_url, token = _conn(proxy)
	eff = _effective_groups()
	if groups is not _ALL:
		wanted = set(groups)
		eff = [g for g in eff if g["name"] in wanted]
	return _post(admin_url, token, "groups", {"groups": eff, "catalog": _public_catalog()})


def push_users(proxy, users=_ALL):
	"""Upsert users to one proxy. users=_ALL → every user; a list of Grove User names → just
	those (the agent HSETs each; other users are left untouched)."""
	_p, admin_url, token = _conn(proxy)
	eff = _effective_users(None if users is _ALL else users)
	return _post(admin_url, token, "users", {"users": eff})


def push_keys(proxy, keys=_ALL):
	"""Upsert keys to one proxy. keys=_ALL → the whole set; a list of API Key
	names → just those (the agent HSETs each; other keys are left untouched)."""
	_p, admin_url, token = _conn(proxy)
	eff = _effective_keys()
	if keys is not _ALL:
		wanted = set(keys)
		eff = [k for k in eff if k["prefix"] in wanted]
	return _post(admin_url, token, "keys", {"keys": eff})


_DELETION_PATHS = {"Key": "keys", "User": "users"}


def push_deletions(proxy, deletions):
	"""Drop records the control plane no longer has. The only pruning path there is — every other
	endpoint upserts — so a revoked key keeps working on a box until this lands."""
	_p, admin_url, token = _conn(proxy)
	removed = 0
	for record_type, path in _DELETION_PATHS.items():
		ids = [d.record_id for d in deletions if d.record_type == record_type]
		if ids:
			removed += _post(admin_url, token, path, {"ids": ids}, method="DELETE").get("count", 0)
	return {"count": removed}


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
	"""Push the COMPLETE group + user + key set + routing table to each target proxy, and the
	replica table to each Active ingress. proxies=None → all Active. Used by buttons, proxy
	activation, provisioning.

	Naming a subset of proxies leaves the ingresses alone: that call means "this one box missed
	something", and an ingress's table has nothing to do with a gateway's."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=60):  # forced → queue behind an in-flight run
		return None
	try:
		all_active = _active_proxies()
		active = proxies or all_active
		ingresses = [] if proxies else _active_ingresses()
		if not (active or ingresses):
			return None

		snaps = {d: _snapshot_dirty(d) for d in _DIRTY_DOCTYPES}
		# A complete push still cannot prune, so the pending deletions ride along with it.
		deletions = _pending_deletions()

		ok = 0
		for proxy in active:
			res = _push_and_classify(proxy, _ALL, _ALL, _ALL, deletions, _ALL)
			doc.append("results", {"server_type": "Gateway Server", "server": proxy, **res})
			ok += res["success"]
		for ingress in ingresses:
			res = _push_replicas_and_classify(ingress)
			doc.append("results", {"server_type": "Ingress Server", "server": ingress, **res})
			ok += res["success"]
		_finalize(doc, list(active) + ingresses, ok)

		# A full run that reached every Active proxy has projected all current
		# groups, users and keys, so the dirty ones it covered can clear. (Subset runs
		# clear nothing global; routes are not dirty-gated.)
		if doc.status == "Success" and set(active) >= set(all_active):
			for doctype, snap in snaps.items():
				_clear_unchanged(doctype, snap)
			_clear_deletions(deletions)

		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def sync_dirty(trigger="Scheduled"):
	"""Background job (cron): push dirty groups + dirty users + dirty keys + the full route table
	for every deployment to the relevant Active proxies, then clear each item that landed
	(failures stay dirty → retried next tick). Routes are NOT dirty-gated — they're pushed
	every run (idempotent). Skips (no doc) when there's nothing to push (nothing dirty and
	no deployments).

	A group edit dirties ONE row here whatever it grants, and so does a user's access change or
	budget flip — the fan-out both used to cause is exactly what the split into their own records
	removed. Only minting a credential dirties a key; revoking one deletes it, which is a Gateway
	Deletion instead."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=0):  # scheduled → skip if a run is in flight
		return None
	try:
		dirty = {d: _snapshot_dirty(d) for d in _DIRTY_DOCTYPES}
		deletions = _pending_deletions()
		# Routes come from Model Deployments AND standalone serving Pods (Running).
		has_deps = bool(frappe.db.count("Model Deployment")) or bool(
			frappe.db.count("Pod", {"status": "Running"})
		)
		if not any(dirty.values()) and not deletions and not has_deps:
			return None

		all_active = set(_active_proxies())
		# Ingresses take the same tick. Their table is derived from the same deployments, so
		# whatever moved a route on a gateway moved one here too.
		ingresses = _active_ingresses() if has_deps else []
		if not (all_active or ingresses):
			return None  # work exists but no Active box can receive it yet

		# Everything here is global — no record is scoped to one proxy — so a doctype with
		# anything dirty goes to every Active proxy, and routes go every tick regardless.
		targets_by_doctype = {d: all_active if rows else set() for d, rows in dirty.items()}
		route_targets = all_active if has_deps else set()
		deletion_targets = all_active if deletions else set()
		targets = route_targets.union(deletion_targets, *targets_by_doctype.values())

		ok_by_proxy = {}
		total_ok = 0
		for proxy in sorted(targets):
			args = [
				[row.name for row in dirty[d]] if proxy in targets_by_doctype[d] else None
				for d in _DIRTY_DOCTYPES
			]
			models_arg = _ALL if proxy in route_targets else None
			res = _push_and_classify(proxy, *args, deletions, models_arg)
			doc.append("results", {"server_type": "Gateway Server", "server": proxy, **res})
			ok_by_proxy[proxy] = bool(res["success"])
			total_ok += res["success"]
		for ingress in ingresses:
			res = _push_replicas_and_classify(ingress)
			doc.append("results", {"server_type": "Ingress Server", "server": ingress, **res})
			total_ok += res["success"]
		_finalize(doc, list(targets) + ingresses, total_ok)

		# Each doctype clears only once every proxy it targeted accepted the push.
		for doctype, rows in dirty.items():
			if rows and all(ok_by_proxy.get(p) for p in targets_by_doctype[doctype]):
				_clear_unchanged(doctype, rows)
		if deletions and all(ok_by_proxy.get(p) for p in deletion_targets):
			_clear_deletions(deletions)

		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def _push_replicas_and_classify(ingress):
	"""One ingress's replica table, reported in the same shape a gateway push is, so a run's log
	reads as one list of targets rather than two."""
	start = time.monotonic()
	reachable, success, http_status, error = 1, 0, 0, None
	detail = []
	try:
		result = sync_replicas(ingress)
		detail.append(f"replicas:{result.get('models', '?')}")
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
		"http_status": http_status,
		"error": error,
		"duration_ms": int((time.monotonic() - start) * 1000),
		"detail": " ".join(detail),
	}


def _push_and_classify(proxy, groups, users, keys, deletions, models):
	"""Push the requested groups and/or users and/or keys and/or deletions and/or routes to one
	proxy (None = skip that push); report reachability and success separately so the log
	distinguishes 'proxy down' from 'up but rejected'.

	Pushed in pointer order — group, then user, then key — because each names the one before it.
	A record that landed ahead of the one it points at would resolve against nothing and 403
	until the next tick. Deletions go after the upserts, so a run is never briefly missing a
	record it is about to write."""
	start = time.monotonic()
	reachable, success, http_status, error = 1, 0, 0, None
	detail = []
	try:
		if groups is not None:
			r = push_groups(proxy, groups)
			detail.append(f"groups:{r.get('count', '?')}")
		if users is not None:
			r = push_users(proxy, users)
			detail.append(f"users:{r.get('count', '?')}")
		if keys is not None:
			r = push_keys(proxy, keys)
			detail.append(f"keys:{r.get('count', '?')}")
		if deletions:
			r = push_deletions(proxy, deletions)
			detail.append(f"deleted:{r.get('count', '?')}")
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
	return frappe.get_all("Gateway Server", filters={"status": "Active"}, pluck="name")


def _active_ingresses():
	"""Ingresses ready to take a replica table. One with no Network is skipped rather than thrown
	on — a scheduled run must not die over one misconfigured box."""
	return frappe.get_all(
		"Ingress Server", filters={"status": "Active", "network": ("is", "set")}, pluck="name"
	)


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


def _pending_deletions():
	"""Records every proxy should drop, oldest first. There is no dirty flag to clear here — the
	row IS the flag, so it survives until a run has removed the record everywhere."""
	return frappe.get_all(
		"Gateway Deletion", fields=["name", "record_type", "record_id"], order_by="creation asc"
	)


def _clear_deletions(deletions):
	"""Drop the tombstones a run has pushed to every Active proxy. Deleted rather than flagged:
	a record removed everywhere has nothing left to say."""
	frappe.db.delete("Gateway Deletion", {"name": ("in", [d.name for d in deletions])})


def _clear_unchanged(doctype, rows, only=None):
	"""Clear `dirty` for snapshot rows not modified since the snapshot — an edit
	during the run bumps `modified`, so that item stays dirty for the next run
	instead of being wrongly marked synced."""
	for r in rows:
		if only is not None and r.name not in only:
			continue
		if frappe.db.get_value(doctype, r.name, "modified") == r.modified:
			frappe.db.set_value(doctype, r.name, "dirty", 0, update_modified=False)
