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

Two planes, and what each is given is what keeps them apart. A GATEWAY takes groups, users, keys
and a global route table. An INGRESS takes one thing — the replica table for the boxes it owns —
and there is no endpoint on it to send anything else to.

Two paths:
  * full_sync(proxies, ingresses)  — push the COMPLETE state to the named boxes, or to every
    Active one when a list is not given. Manual (buttons), activation, and provisioning use this.
    An empty list means "none of that kind", which is how an ingress-only run asks for no gateway
    work.
  * sync_dirty()                   — background job (cron): push ONLY the groups/users/keys
    flagged `dirty` since the last sync, then clear their flag. Failures stay dirty and are
    retried on the next tick. Routes and replica tables are not dirty-gated; they go every tick,
    which is what makes this the repair pass for both planes.

Both log one Gateway Sync doc per run with a child row per TARGET — naming the doctype as well as
the box, because the two planes take different pushes — and both serialize against each other via
a MariaDB advisory lock so a slow run can't land a stale write after a newer one. Every path that
reaches a box goes through here, so a push that left no row did not happen."""

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


def _ingress_targets():
	"""{Inference Server name: the ingress row its engines fold into, or None}.

	A key with None means the box is owned by an ingress the gateway cannot use right now. Those
	engines are dropped, NOT dialled directly: a box that names an ingress is reached through it or
	not at all. The fallback would appear to work today, while every box still carries its own
	front, and become a black hole the moment phase 4 deletes it — a behaviour that changes under
	you between phases is worse than one that is dark and says so.

	One row per (model, ingress) rather than per replica is the whole point: the gateway learns
	that Mumbai has two ingresses, never which boxes are behind them, so a pod restarting there is
	invisible to a gateway in Singapore.

	No Fleet Zone is the exception, and it is a different condition: no box in the fleet has a name
	yet, which is the pre-TLS setup where everything is dialled by IP. Ownership is ignored
	entirely there rather than taking the whole fleet dark over a setting nobody has filled in."""
	zone = frappe.db.get_single_value("Grove Settings", "fleet_zone")
	if not zone:
		return {}
	ingresses = {
		row["name"]: row
		for row in frappe.get_all(
			"Ingress Server", filters={"status": "Active"}, fields=["name", "region"]
		)
	}
	targets = {}
	for server in frappe.get_all(
		"Inference Server", filters={"ingress": ("is", "set")}, fields=["name", "ingress"]
	):
		ingress = ingresses.get(server["ingress"])
		if not ingress:
			targets[server["name"]] = None
			continue
		targets[server["name"]] = {
			# No path: access.lua appends the client's request_uri, exactly as it does for an
			# engine. The ingress then appends the same thing again onto its own replica's URL.
			"engine_url": f"https://{ingress['name']}.{zone}",
			# The gateway presents this to prove it is a gateway. It is NOT the admin token.
			"internal_key": frappe.get_doc("Ingress Server", ingress["name"]).get_password(
				"data_token", raise_exception=False
			) or "",
			"healthy": True,
			"region": ingress["region"] or "",
			# Both name the ingress: buildRequestID records where the gateway SENT the request,
			# and the ingress's own log records where it landed.
			"deployment": ingress["name"],
			"server": ingress["name"],
			"kind": "ingress",
		}
	return targets


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
	targets = _ingress_targets()
	# One row per (model, ingress), so several deployments behind one ingress fold together
	# instead of each getting a row that names the same URL.
	folded = {}
	for d in deps:
		routes.setdefault(d.model, [])
		if d.status != "Active":
			continue
		# --max-num-seqs is what this engine runs at once; past it vLLM queues. Blank resolves to
		# the same default the serve command uses — the two have to agree or the cap is not the
		# engine's.
		capacity = int(d.max_num_seqs or DEFAULT_MAX_NUM_SEQS)
		if d.inference_server in targets:
			target = targets[d.inference_server]
			if target is None:
				continue  # owned by an ingress that cannot take traffic — dark, not dialled direct
			# Advisory here, authoritative on the ingress. The gateway sums what sits behind an
			# ingress to choose between ingresses; the ingress applies the exact per-replica gate
			# and 429s the excess, which is why two gateways cannot jointly overrun a replica.
			row = folded.setdefault((d.model, target["server"]), {**target, "capacity": 0})
			row["capacity"] += capacity
			continue
		internal_key = frappe.get_doc("Model Deployment", d.name).get_password("internal_api_key") or ""
		routes[d.model].append({
			"engine_url": d.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": capacity,
			# Which placement was chosen (request-id target part, access-log deployment=).
			# A box can serve the same model twice, so the server alone cannot name an engine.
			"deployment": d.name,
			"server": d.inference_server or d.name,  # which box it is on
			"kind": "direct",
		})
	for (model, _ingress), row in folded.items():
		routes[model].append(row)

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
			# Always direct: a pod has no Machine and so no Network, and cannot sit behind an
			# ingress. Provider TLS already covers the hop.
			"kind": "direct",
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

def full_sync(proxies=None, trigger="Manual", ingresses=None):
	"""Push the COMPLETE group + user + key set + routing table to each target proxy, and the
	replica table to each target ingress. Used by buttons, proxy activation, provisioning.

	Both targets default to every Active box, and both can be named instead. The asymmetry is
	deliberate: a gateway's table is GLOBAL — every gateway holds a row for every model — so a
	route change reaches all of them. An ingress's table holds only the replicas it OWNS, so a
	deployment change reaches exactly one ingress and pushing it to the rest is a table they
	already have.

	Naming a subset of proxies with no ingresses leaves the ingresses alone: that call means
	"this one gateway missed something", and a gateway's table has nothing to do with an
	ingress's."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=60):  # forced → queue behind an in-flight run
		return None
	try:
		all_active = _active_proxies()
		# `is None` and not truthiness: an empty list is a caller saying "no boxes of this kind",
		# which is how an ingress-only run asks for no gateway work. `or` would read that as
		# "unspecified" and push to the whole fleet.
		active = all_active if proxies is None else proxies
		if ingresses is None:
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


def active_among(ingresses):
	"""Those of these ingresses that can actually take a push — Active, and with a Network to
	build a table from. A Broken or half-configured one is skipped rather than thrown on."""
	names = {name for name in ingresses if name}
	if not names:
		return []
	return frappe.get_all(
		"Ingress Server",
		filters={"name": ("in", list(names)), "status": "Active", "network": ("is", "set")},
		pluck="name",
	)


def owning_ingresses(inference_servers):
	"""The Active ingresses that own any of these boxes — the only ones a change to those boxes
	moves. Everything else in the fleet, including other ingresses in the same Network, holds a
	table this push would not change.

	Empty for a box that names no ingress, which is a box the gateways still dial directly."""
	names = [name for name in inference_servers if name]
	if not names:
		return []
	return active_among(
		frappe.get_all(
			"Inference Server",
			filters={"name": ("in", names), "ingress": ("is", "set")},
			pluck="ingress",
		)
	)


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


def _finalize(doc, targets, ok):
	"""Both planes counted together — a run's targets are its gateways plus its ingresses."""
	doc.targets_total = len(targets)
	doc.targets_ok = ok
	doc.status = "Success" if ok == len(targets) else ("Failed" if ok == 0 else "Partial")
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
