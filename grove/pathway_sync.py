# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Project Grove state (groups + users + keys + routing table) into each Gateway Server's
local Redis, and the replica table into each Ingress Server, via the agent's token-gated
admin API. Grove is the source of truth.

The push is desired state, whole, gated by hashes the AGENT stores (`grove:state_hash`):
each tick builds the snapshot, reads the box's stored hashes, and sends only the
sections whose hash the box does not already hold. Absence prunes — a deleted group,
revoked key or retired model simply stops being named and the agent removes it. A box
that loses its Redis loses its hashes with it, so the next tick re-pushes everything;
there is no other repair path and none is needed.

Sections: `groups` (records + the pooled public catalog) and `routes` travel whole.
`users` and `keys` scale with customer count, so they are split into 256 buckets
(`bucket_of`) hashed independently — one key minted re-pushes one bucket, not the
population.

Two planes, and what each is given is what keeps them apart. A GATEWAY takes the full
snapshot. An INGRESS takes one thing — the replica table for the boxes it owns.

Two entry points:
  * sync_projection() — the cron tick, every minute, and the ONLY automatic path there is:
    hash-gate each Active box, push drift only. Logs a Pathway Sync doc only when something was
    actually pushed (or failed). Nothing in a doctype hook, a provision or a pod lifecycle
    pushes inline — they move the state, and the next tick carries it.
  * full_sync(proxies, ingresses) — force-push the complete snapshot to the named boxes, or to
    every Active one. Operator buttons only; an empty proxies list means "no gateway work",
    which is how the ingress-only button asks.

Both serialize on the Pathway Sync doc's advisory lock so a slow run can't land a stale
write after a newer one. Every path that reaches a box goes through here, so a push
that left no row did not happen."""

import hashlib
import json
import time

import requests

import frappe

from grove.access import model_rows
from grove.net import private_url
from grove.serving.base import engine_class

TIMEOUT = 10


def _conn(name, doctype="Gateway Server"):
	"""The admin API of one box, whichever plane it is on. Both kinds derive admin_url the same
	way and both gate /admin on the same token — what differs is which sections they are sent."""
	p = frappe.get_doc(doctype, name)
	if not p.admin_url:
		frappe.throw(f"{doctype} {name} has no admin_url")
	return p, p.admin_url.rstrip("/"), (p.get_password("admin_token") or "")


# Every key whose value is a live credential. Redacted before a payload is written to a Gateway
# Sync Row, which is readable by anyone who can open the Desk: internal_key is the engine's own key
# on a replica row and the ingress's data token on a gateway row, and key_hash is what a gateway
# looks a customer's credential up by.
_SECRET_KEYS = frozenset({"internal_key", "key_hash", "admin_token", "data_token", "api_secret"})
# A log row, not an archive. A fleet-sized route table would otherwise put megabytes into the
# table and the form that renders it.
_PAYLOAD_LIMIT = 8000


def _redact(value):
	"""A payload with every secret replaced by a marker, structure intact.

	Marked rather than dropped: "internal_key": "***" says the key was sent, which is what you are
	checking when you read one of these back. A missing field would read as a push that forgot it."""
	if isinstance(value, dict):
		return {k: ("***" if k in _SECRET_KEYS and v else _redact(v)) for k, v in value.items()}
	if isinstance(value, list):
		return [_redact(item) for item in value]
	return value


def _record_payload(path, payload):
	"""Keep what was just sent, for the row this push will land on. On frappe.local so it is scoped
	to this job — a module global would bleed between runs sharing a worker."""
	log = getattr(frappe.local, "grove_sync_payloads", None)
	if log is not None:
		log.append({"push": path, "body": _redact(payload)})


def _collected_payload():
	"""What this target was sent, as text for the row. Truncated rather than trimmed field by
	field: a reader wants to see the shape, and a table too big to store is itself worth seeing."""
	log = getattr(frappe.local, "grove_sync_payloads", None) or []
	text = frappe.as_json(log)
	if len(text) > _PAYLOAD_LIMIT:
		return f"{text[:_PAYLOAD_LIMIT]}\n… truncated, {len(text)} characters in full"
	return text


def _post(admin_url, token, path, payload, method="POST"):
	_record_payload(path, payload)
	r = requests.request(
		method,
		f"{admin_url}/{path}",
		json=payload,
		headers={"X-Grove-Admin-Token": token, "Content-Type": "application/json"},
		timeout=TIMEOUT,
	)
	r.raise_for_status()
	return r.json()


def remote_hashes(admin_url, token):
	"""The hash map the box stored on its last accepted push. Empty on a fresh or wiped
	Redis, which is what makes every section read as drift and heal."""
	r = requests.get(
		f"{admin_url}/state-hash", headers={"X-Grove-Admin-Token": token}, timeout=TIMEOUT
	)
	r.raise_for_status()
	return r.json().get("hashes") or {}


# --- Desired state -----------------------------------------------------------

def _effective_groups():
	"""Every Grove User Group projected for the gateway: what it grants. One record per group
	however many keys point at it — the reason the group is not flattened onto each key."""
	granted = model_rows("Grove User Group")
	return [
		{
			"name": name,
			"models": ",".join(granted.get(name, {}).get("models", [])),
		}
		for name in sorted(frappe.get_all("Grove User Group", pluck="name"))
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


def _effective_users():
	"""Every Grove User projected for the gateway: their group, their own allow/deny, and whether
	they are over their monthly budget. One record per person however many keys they hold — the
	reason none of this is flattened onto the keys.

	The gateway keeps no rate counters. The only limit is the monthly token budget, which the
	control plane flags here and the gateway honours as a 429. Holding it on the person is what
	stops a blocked user minting a fresh, unblocked key."""
	deltas = model_rows("Grove User")
	users = frappe.get_all(
		"Grove User", fields=["name", "user", "user_group", "rate_limited"]
	)
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
		for u in sorted(users, key=lambda u: u.name)
	]


def _effective_keys():
	"""Every LIVE API Key projected for the gateway. A key is a pointer to whoever holds it and
	nothing else — what they may call and whether they are over budget belongs to the person and
	is pushed as their user record.

	Revoked keys are not projected at all: absent from their bucket, the push prunes them off
	every box. The row stays in Grove as the record of a credential that existed."""
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


def _engine_kinds():
	"""Every Engine Image's kind, resolved once per snapshot rather than per route row — there is a
	handful of images against every placement in the fleet."""
	return {
		image["name"]: image["engine_kind"]
		for image in frappe.get_all("Engine Image", fields=["name", "engine_kind"])
	}


def _capacity(row, kinds):
	"""What this placement's engine runs at once. Past it the engine queues, where the gateway can
	neither see the wait nor spend it on a replica — so it is admission control, not a hint. A
	placement naming no image reads as vllm, which is what every one predating the field is."""
	kind = kinds.get(row.get("engine_image")) or "vllm"
	return int(row.get("max_num_seqs") or engine_class(kind).default_concurrency)


def _gateway_routes():
	"""deploy:<model> table (global — the same for every gateway): every model with an Active
	engine maps to its engines. A model with none is simply absent; the push prunes its key,
	so it drops out of Redis and /v1/models on the next tick."""
	kinds = _engine_kinds()
	deps = frappe.get_all(
		"Model Deployment",
		filters={"status": "Active"},
		fields=["name", "model", "engine_url", "inference_server", "max_num_seqs", "engine_image"],
	)
	# Which endpoints the model answers on. Stamped per row because deploy:<model> is the only
	# thing pushed per model — a separate record would be a new namespace and a new push for one
	# short string. Blank means unrestricted, which is what a Model predating the field reads as.
	modality = {
		m.name: m.modality or "" for m in frappe.get_all("Model", fields=["name", "modality"])
	}
	routes = {}
	targets = _ingress_targets()
	# One row per (model, ingress), so several deployments behind one ingress fold together
	# instead of each getting a row that names the same URL.
	folded = {}
	for d in deps:
		# --max-num-seqs is what this engine runs at once; past it vLLM queues. Blank resolves to
		# the same default the serve command uses — the two have to agree or the cap is not the
		# engine's.
		capacity = _capacity(d, kinds)
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
		routes.setdefault(d.model, []).append({
			"engine_url": d.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": capacity,
			# Which placement was chosen (request-id target part, access-log deployment=).
			# A box can serve the same model twice, so the server alone cannot name an engine.
			"deployment": d.name,
			"server": d.inference_server or d.name,  # which box it is on
			"kind": "direct",
			"modality": modality.get(d.model, ""),
		})
	for (model, _ingress), row in folded.items():
		routes.setdefault(model, []).append({**row, "modality": modality.get(model, "")})

	# Standalone serving Pods (a vLLM image serving the Model directly — no Model Deployment)
	# register the same way: deploy:<model> → engine. Only Running pods with a derived
	# engine_url contribute; others are dropped so the agent 503s instead of routing to a
	# dead endpoint. A model served by both an MD and a Pod gets both engines (load-balanced).
	pods = frappe.get_all(
		"Pod",
		filters={"status": "Running"},
		fields=["name", "model", "engine_url", "max_num_seqs", "engine_image"],
	)
	for p in pods:
		# A pod with no Model serves something the gateway has no route key for (an ASR
		# container, say) — it is reached directly, not through deploy:<model>.
		if not (p.model and p.engine_url):
			continue
		internal_key = frappe.get_doc("Pod", p.name).get_password("api_key") or ""
		routes.setdefault(p.model, []).append({
			"engine_url": p.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": _capacity(p, kinds),
			# A pod IS its own placement — it carries no separate deployment doc, so both
			# fields are the pod. Kept explicit so consumers never special-case a pod route.
			"deployment": p.name,
			"server": p.name,
			# Always direct: a pod has no Machine and so no Network, and cannot sit behind an
			# ingress. Provider TLS already covers the hop.
			"kind": "direct",
			"modality": modality.get(p.model, ""),
		})
	for rows in routes.values():
		rows.sort(key=lambda r: r["deployment"])  # stable hash whatever the query order
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

	The same shape the gateway's table has, so the agent's handler is reused whole — what narrows
	is the scope. An ingress is told about its own boxes and no others, so a pod restarting in
	another region is not an event it ever sees. That is the point of the split: replica topology
	never leaves its own Network.

	Only the models this ingress can actually serve — the payload is the whole table, so anything
	it does not name is pruned and /pick answers 503 no-replica.

	Pods are absent by construction — they have no Machine and so no ingress, and stay on the
	gateway's direct route kind."""
	owned = _owned_boxes(ingress_name)
	routes = {}
	if not owned:
		return routes

	kinds = _engine_kinds()
	deployments = frappe.get_all(
		"Model Deployment",
		filters={"status": "Active", "inference_server": ("in", list(owned))},
		fields=["name", "model", "engine_url", "inference_server", "max_num_seqs", "engine_image"],
	)
	for deployment in deployments:
		engine_url = private_url(deployment.engine_url, owned[deployment.inference_server])
		if not engine_url:
			continue
		internal_key = frappe.get_doc("Model Deployment", deployment.name).get_password("internal_api_key") or ""
		routes.setdefault(deployment.model, []).append({
			"engine_url": engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": _capacity(deployment, kinds),
			# No `server`: which box an engine sits on is the gateway's request-id part, and the
			# ingress has no use for it — it already holds the box's address in engine_url.
			"deployment": deployment.name,
		})
	for rows in routes.values():
		rows.sort(key=lambda r: r["deployment"])
	return routes


# --- Snapshot + hash gate ----------------------------------------------------

def bucket_of(record_id):
	"""Which of the 256 state buckets a user or key belongs to. The agent prunes bucket members
	by the same rule, so the two sides must never disagree on it."""
	return hashlib.sha256(str(record_id).encode()).hexdigest()[:2]


def _hash(content):
	"""sha256 of the canonical JSON. The agent stores this verbatim and never recomputes it, so
	only THIS function has to be deterministic — hence the sorts in the builders above."""
	return hashlib.sha256(
		json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def _flat_section(content):
	return {**content, "hash": _hash(content)}


def _bucketed_section(records, id_field):
	buckets = {}
	for record in records:
		buckets.setdefault(bucket_of(record[id_field]), []).append(record)
	return {"buckets": {
		label: {"records": rows, "hash": _hash({"records": rows})}
		for label, rows in buckets.items()
	}}


def gateway_snapshot():
	"""The complete desired state of one gateway — the same for every gateway, so a run builds
	it once. groups and routes travel whole; users and keys are bucketed so one record's change
	re-pushes one bucket, not the population."""
	return {
		"groups": _flat_section({"records": _effective_groups(), "catalog": _public_catalog()}),
		"users": _bucketed_section(_effective_users(), "name"),
		"keys": _bucketed_section(_effective_keys(), "key_hash"),
		"routes": _flat_section({"table": _gateway_routes()}),
	}


def ingress_snapshot(ingress):
	"""The complete desired state of one ingress: its replica table and nothing else — there is
	no keys, users or groups section on that plane to send anything else in."""
	return {"routes": _flat_section({"table": _replicas_for_ingress(ingress)})}


def _delta(snapshot, remote):
	"""The sections/buckets whose hash the box does not already hold — what a non-forced run
	pushes. A bucket the box hashes but the snapshot no longer has (its last record deleted) is
	sent explicitly empty, so the agent prunes its members instead of holding them forever."""
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


def _describe(delta, response):
	"""One short string per push for the row's `detail`: which sections went (bucket counts in
	brackets) and how many records the agent says it wrote."""
	counts = (response or {}).get("counts") or {}
	parts = []
	for section in ("groups", "users", "keys", "routes"):
		if section not in delta:
			continue
		buckets = delta[section].get("buckets")
		label = f"{section}[{len(buckets)}]" if buckets is not None else section
		if section in counts:
			label = f"{label}:{counts[section]}"
		parts.append(label)
	return " ".join(parts)


def _sync_target(server_type, name, snapshot, force):
	"""Bring one box to the snapshot. Returns a result row, or None when the box already holds
	it (nothing pushed, nothing to log). Reachability and success are reported separately so the
	log distinguishes 'box down' from 'up but rejected'."""
	start = time.monotonic()
	reachable, success, http_status, error, detail = 1, 0, 0, None, ""
	frappe.local.grove_sync_payloads = []
	try:
		_doc, admin_url, token = _conn(name, server_type)
		delta = snapshot
		if not force:
			delta = _delta(snapshot, remote_hashes(admin_url, token))
			if not delta:
				return None
		response = _post(admin_url, token, "state", delta)
		detail = _describe(delta, response)
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
		"detail": detail,
		# Recorded even on failure — what a rejected push tried to send is the whole question.
		"payload": _collected_payload(),
	}


# --- Sync runs ---------------------------------------------------------------

def sync_projection(trigger="Scheduled", proxies=None, ingresses=None, force=False, wait=0):
	"""Bring every target box to the current desired state. The cron tick (defaults): hash-gate
	each box and push only drift, logging a run doc only when at least one box was pushed or
	failed — a fleet already in sync leaves no doc.

	Both target kinds default to every Active box, and both can be named instead. `is None` and
	not truthiness: an empty list is a caller saying "no boxes of this kind", which is how an
	ingress-only run asks for no gateway work."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=wait):  # scheduled → skip if a run is in flight; forced → queue
		return None
	try:
		active = _active_proxies() if proxies is None else proxies
		if ingresses is None:
			ingresses = [] if proxies else _active_ingresses()
		if not (active or ingresses):
			return None

		snapshot = gateway_snapshot() if active else None
		ok = 0
		stamped = False
		for proxy in active:
			res = _sync_target("Gateway Server", proxy, snapshot, force)
			stamped |= _stamp_synced("Gateway Server", proxy, res)
			if res is None:
				continue
			doc.append("results", {"server_type": "Gateway Server", "server": proxy, **res})
			ok += res["success"]
		for ingress in ingresses:
			res = _sync_target("Ingress Server", ingress, ingress_snapshot(ingress), force)
			stamped |= _stamp_synced("Ingress Server", ingress, res)
			if res is None:
				continue
			doc.append("results", {"server_type": "Ingress Server", "server": ingress, **res})
			ok += res["success"]

		if doc.results:
			_finalize(doc, len(doc.results), ok)
			frappe.db.commit()
			return doc.name
		if stamped:
			frappe.db.commit()
		return None
	finally:
		doc.release_lock()


def check_state(server_type, name):
	"""What a tick would push to this box right now, without pushing it — the Check State
	button's answer. Returns {"in_sync": bool, "drift": ["groups", "keys[2]", ...]}, where a
	bracketed count is how many buckets of that section differ."""
	snapshot = gateway_snapshot() if server_type == "Gateway Server" else ingress_snapshot(name)
	_doc, admin_url, token = _conn(name, server_type)
	delta = _delta(snapshot, remote_hashes(admin_url, token))
	drift = [
		f"{section}[{len(content['buckets'])}]" if "buckets" in content else section
		for section, content in delta.items()
	]
	return {"in_sync": not delta, "drift": sorted(drift)}


def full_sync(proxies=None, trigger="Manual", ingresses=None, wait=60):
	"""Force-push the complete snapshot, skipping the hash gate. The operator buttons use this —
	a button means "this box missed something", so it waits for an in-flight run rather than
	skipping."""
	return sync_projection(
		trigger=trigger, proxies=proxies, ingresses=ingresses, force=True, wait=wait
	)


# --- helpers ---------------------------------------------------------------

def _active_proxies():
	return frappe.get_all("Gateway Server", filters={"status": "Active"}, pluck="name")


def _active_ingresses():
	"""Ingresses ready to take a replica table. One with no Network is skipped rather than thrown
	on — a scheduled run must not die over one misconfigured box."""
	return frappe.get_all(
		"Ingress Server", filters={"status": "Active", "network": ("is", "set")}, pluck="name"
	)


def _stamp_synced(doctype, name, res):
	"""Record that this box was checked and holds the desired state — on an in-sync skip (no row
	is written, so this is the only trace) and on a successful push alike. Never on failure: the
	timestamp going stale is what says a box has been failing, and the Failed rows say why."""
	if res is not None and not res["success"]:
		return False
	frappe.db.set_value(
		doctype, name, "last_synced_at", frappe.utils.now_datetime(), update_modified=False
	)
	return True


def _new_run(sync_type, trigger):
	doc = frappe.new_doc("Pathway Sync")
	doc.run_at = frappe.utils.now_datetime()
	doc.sync_type = sync_type
	doc.trigger = trigger
	return doc


def _finalize(doc, total, ok):
	"""Both planes counted together — a run's targets are the boxes it actually pushed."""
	doc.targets_total = total
	doc.targets_ok = ok
	doc.status = "Success" if ok == total else ("Failed" if ok == 0 else "Partial")
	doc.insert(ignore_permissions=True)
