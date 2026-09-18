# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Project Grove state into each Gateway Server's local Redis, and the replica table into each
Ingress Server, via the agent's token-gated admin API. Grove is the source of truth.

The push is desired state, whole, gated by hashes the AGENT stores (`grove:state_hash`): each tick
builds the snapshot, reads the box's hashes, and sends only the sections it does not already hold.
Absence prunes — a deleted group or revoked key stops being named and the agent removes it. A box
that loses its Redis loses its hashes with it, so the next tick re-pushes everything; that is the
only repair path there is.

`groups` and `routes` travel whole. `users` and `keys` scale
with customer count, so they split into 256 buckets (`bucket_of`) hashed independently — one key
minted re-pushes one bucket, not the population.

What each plane is given is what keeps them apart: a GATEWAY takes the full snapshot with only its
own Geography's routes, an INGRESS takes only the replica table for the boxes it owns.

Two entry points:
  * sync_projection() — the cron tick, and the ONLY automatic path there is. Logs a Pathway Sync
    doc only when something was pushed or failed. Nothing pushes inline; state moves, and the next
    tick carries it.
  * full_sync(proxies, ingresses) — force-push to the named boxes, or every Active one. Operator
    buttons only; an empty proxies list means "no gateway work", which is how the ingress-only
    button asks.

Both serialize on the Pathway Sync doc's advisory lock, so a slow run cannot land a stale write
after a newer one. Every path that reaches a box goes through here."""

import hashlib
import json
import time

import requests

import frappe

from grove.access import group_rows, model_rows
from grove.naming import short_name
from grove.net import private_url
from grove.serving.base import engine_class

TIMEOUT = 10


def _conn(name, doctype="Gateway Server"):
	"""The admin API of one box, whichever plane it is on. Both derive admin_url the same way and
	gate /admin on the same token; only the sections differ."""
	p = frappe.get_doc(doctype, name)
	if not p.admin_url:
		frappe.throw(f"{doctype} {name} has no admin_url")
	return p, p.admin_url.rstrip("/"), (p.get_password("admin_token") or "")


# Redacted before a payload is written to a Gateway Sync Row, which anyone who can open the Desk
# can read.
_SECRET_KEYS = frozenset({"internal_key", "key_hash", "admin_token", "data_token", "api_secret"})
# A log row, not an archive: a fleet-sized route table is megabytes.
_PAYLOAD_LIMIT = 8000


def _redact(value):
	"""Secrets replaced by a marker, structure intact. Marked rather than dropped: `"***"` says the
	key WAS sent, where a missing field would read as a push that forgot it."""
	if isinstance(value, dict):
		return {k: ("***" if k in _SECRET_KEYS and v else _redact(v)) for k, v in value.items()}
	if isinstance(value, list):
		return [_redact(item) for item in value]
	return value


def _record_payload(path, payload):
	"""On frappe.local so it is scoped to this job: a module global would bleed between runs
	sharing a worker."""
	log = getattr(frappe.local, "grove_sync_payloads", None)
	if log is not None:
		log.append({"push": path, "body": _redact(payload)})


def _collected_payload():
	"""What this target was sent, as text for the row. Truncated rather than trimmed field by
	field: a table too big to store is itself worth seeing."""
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
	"""The hash map the box stored on its last accepted push. Empty on a wiped Redis, which is what
	makes every section read as drift and heal."""
	r = requests.get(
		f"{admin_url}/state-hash", headers={"X-Grove-Admin-Token": token}, timeout=TIMEOUT
	)
	r.raise_for_status()
	return r.json().get("hashes") or {}


# --- Desired state -----------------------------------------------------------

def _effective_groups():
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


def _effective_users():
	"""Every Grove User projected for the gateway. One record per person however many keys they
	hold — the reason none of this is flattened onto the keys.

	The gateway keeps no rate counters: the only limit is the monthly token budget, flagged here
	and honoured as a 429. Holding it on the PERSON stops a blocked user minting a fresh key."""
	deltas = model_rows("Grove User")
	memberships = group_rows()
	users = frappe.get_all("Grove User", fields=["name", "user", "rate_limited", "log_payloads", "geography"])
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
		}
		for u in sorted(users, key=lambda u: u.name)
	]


def _effective_keys():
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


def _ingress_targets(geography):
	"""{Inference Server name: the ingress row its engines fold into, or None}.

	None means the box is owned by an ingress the gateway cannot use right now. Those engines are
	dropped, NOT dialled directly: a box that names an ingress is reached through it or not at all.
	The fallback would work today and become a black hole the moment phase 4 deletes the box's own
	front.

	One row per (model, ingress) rather than per replica is the point: the gateway learns that
	Mumbai has two ingresses, never which boxes are behind them.

	No Fleet Zone is a different condition — no box has a name yet, the pre-TLS setup where
	everything is dialled by IP — so ownership is ignored rather than taking the fleet dark."""
	zone = frappe.db.get_value("Geography", geography, "fleet_zone")
	if not zone:
		return {}
	ingresses = {
		row["name"]: row
		for row in frappe.get_all(
			"Ingress Server", filters={"status": "Active", "geography": geography}, fields=["name", "region"]
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
			# No path: access.lua appends the client's request_uri, as it does for an engine, and
			# the ingress appends it again onto its own replica's URL.
			"engine_url": f"https://{short_name(ingress['name'])}.{zone}",
			# The gateway presents this to prove it is a gateway. It is NOT the admin token.
			"internal_key": frappe.get_doc("Ingress Server", ingress["name"]).get_password(
				"data_token", raise_exception=False
			) or "",
			"healthy": True,
			"region": ingress["region"] or "",
			# Both name the ingress: one records where the gateway SENT it, the other where it
			# landed.
			"deployment": short_name(ingress["name"]),
			"server": short_name(ingress["name"]),
			"kind": "ingress",
		}
	return targets


def _engine_kinds():
	"""Resolved once per snapshot, not per route row: a handful of images against every placement
	in the fleet."""
	return {
		image["name"]: image["engine_kind"]
		for image in frappe.get_all("Engine Image", fields=["name", "engine_kind"])
	}


def _deployments():
	"""Resolved once per snapshot, for the same reason as `_engine_kinds`."""
	return {
		deployment["name"]: deployment
		for deployment in frappe.get_all(
			"Model Deployment", fields=["name", "engine_image", "max_num_seqs"]
		)
	}


def _resolve_deployment(replica, deployments):
	"""A replica's effective image and admission cap. The image lives on its deployment; the cap may
	be overridden per replica, where blank means inherit.

	Not cosmetic — `max_num_seqs` IS the gateway's admission cap. A blank left unresolved would
	advertise `engine_class.default_concurrency` while the engine actually runs the deployment's
	number, and the gateway would admit against a cap the engine never had; the ingress's
	authoritative per-replica gate would then 429 traffic the gateway thought it had room for."""
	deployment = deployments.get(replica.get("model_deployment")) or {}
	replica["engine_image"] = deployment.get("engine_image")
	replica["max_num_seqs"] = replica.get("max_num_seqs") or deployment.get("max_num_seqs")
	return replica


def _capacity(row, kinds):
	"""What this placement's engine runs at once. Past it the engine queues, where the gateway can
	neither see the wait nor spend it elsewhere — admission control, not a hint. No image reads as
	vllm, which is what every placement predating the field is."""
	kind = kinds.get(row.get("engine_image")) or "vllm"
	return int(row.get("max_num_seqs") or engine_class(kind).default_concurrency)


def _gateway_routes(geography):
	"""deploy:<model> table for every gateway in `geography`, built only from what runs inside it. A
	model with no Active engine is simply absent; the push prunes its key, so it drops out of Redis
	and /v1/models next tick."""
	if not geography:
		return {}  # a gateway outside every geography serves nothing rather than anyone's
	kinds = _engine_kinds()
	deployments = _deployments()
	deps = [
		_resolve_deployment(replica, deployments)
		for replica in frappe.get_all(
			"Model Replica",
			filters={"status": "Active", "geography": geography},
			fields=[
				"name", "model", "engine_url", "inference_server",
				"max_num_seqs", "model_deployment",
			],
		)
	]
	# Stamped per row because deploy:<model> is the only thing pushed per model — a record of its
	# own would be a new namespace for one short string. Blank means unrestricted.
	models = frappe.get_all(
		"Model", fields=["name", "modality", "model_id", "upstream_model_id", "provider", "published"]
	)
	modality = {m.name: m.modality or "" for m in models}
	# Once, not per model: a handful of providers against thousands of models.
	vendors = _vendor_endpoints(geography)
	upstream = {m.name: _upstream_model(m, m.provider in vendors) for m in models}
	routes = {}
	targets = _ingress_targets(geography)
	# One row per (model, ingress), so deployments behind one ingress fold together instead of
	# each getting a row naming the same URL.
	folded = {}
	for d in deps:
		# Blank resolves to the same default the serve command uses — the two have to agree or
		# the cap is not the engine's.
		capacity = _capacity(d, kinds)
		if d.inference_server in targets:
			target = targets[d.inference_server]
			if target is None:
				continue  # owned by an ingress that cannot take traffic — dark, not dialled direct
			# Advisory here, authoritative on the ingress: the gateway sums to choose BETWEEN
			# ingresses, and the ingress applies the exact per-replica gate. That is why two
			# gateways cannot jointly overrun a replica.
			row = folded.setdefault((d.model, target["server"]), {**target, "capacity": 0})
			row["capacity"] += capacity
			continue
		internal_key = frappe.get_doc("Model Replica", d.name).get_password("internal_api_key") or ""
		routes.setdefault(d.model, []).append({
			"engine_url": d.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": capacity,
			# A box can serve the same model twice, so the server alone cannot name an engine.
			"deployment": d.name,
			"server": d.inference_server or d.name,  # which box it is on
			"kind": "direct",
			"modality": modality.get(d.model, ""),
			"upstream_model": upstream.get(d.model, ""),
		})
	for (model, _ingress), row in folded.items():
		routes.setdefault(model, []).append({
			**row,
			"modality": modality.get(model, ""),
			# Here, not on the ingress: the gateway is the last hop that reads a body.
			"upstream_model": upstream.get(model, ""),
		})

	# Standalone Pods register the same way. Only Running ones with a derived engine_url, so the
	# agent 503s instead of routing to a dead endpoint. A model served by both gets both engines.
	# Every pod serves in the one Pod Geography, so pods are routed there and nowhere else.
	pods = frappe.get_all(
		"Pod",
		filters={"status": "Running"},
		fields=["name", "model", "engine_url", "max_num_seqs", "engine_image"],
	) if geography == frappe.db.get_single_value("Grove Settings", "pod_geography") else []
	for p in pods:
		# A pod with no Model serves something with no route key — reached directly.
		if not (p.model and p.engine_url):
			continue
		internal_key = frappe.get_doc("Pod", p.name).get_password("api_key") or ""
		routes.setdefault(p.model, []).append({
			"engine_url": p.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": _capacity(p, kinds),
			# A pod IS its own placement, so both fields are the pod. Explicit so consumers never
			# special-case a pod route.
			"deployment": p.name,
			"server": p.name,
			# Always direct: a pod has no Network and cannot sit behind an ingress.
			"kind": "direct",
			"modality": modality.get(p.model, ""),
			# The one case a local route carries this: a custom image advertises its own name.
			"upstream_model": upstream.get(p.model, ""),
		})
	_add_vendor_routes(routes, models, vendors, modality, upstream)
	for rows in routes.values():
		rows.sort(key=lambda r: r["deployment"])  # stable hash whatever the query order
	return routes


def _upstream_model(model, is_vendor):
	"""What the upstream is asked for, or "" to send the id the caller used.

	An override always wins — the one thing that reaches a container image advertising its own
	name. A vendor otherwise gets the bare id, since our namespace is not its; anything we run
	ourselves gets "", because an engine is started under the full Grove id."""
	if model.upstream_model_id:
		return model.upstream_model_id
	return model.model_id if is_vendor else ""


def _vendor_endpoints(geography):
	"""Every third party in `geography` we can actually dial. A provider is dialable as a whole — an
	address without a key is not a route."""
	out = {}
	for name in frappe.get_all("Model Provider", filters={"geography": geography}, pluck="name"):
		provider = frappe.get_cached_doc("Model Provider", name)
		# The URL fields ARE the dialect declaration: each front the vendor runs is one field,
		# and a vendor with neither is one we serve ourselves.
		fronts = [
			(url, dialect)
			for url, dialect in (
				(provider.base_url, "openai"),
				(provider.get("anthropic_base_url"), "anthropic"),
			)
			if url
		]
		if not fronts:
			continue
		# get_password, not the field: a Password column reads back as asterisks and would pass
		# any truthiness check while carrying nothing.
		api_key = provider.get_password("api_key", raise_exception=False)
		if api_key:
			out[name] = {
				"fronts": fronts,
				"api_key": api_key,
				"api_version": provider.api_version or "",
			}
	return out


def _add_vendor_routes(routes, models, vendors, modality, upstream):
	"""One row per published Model per front the third party runs — two for a dual-front vendor
	(DeepSeek's /anthropic), so one provider record serves both surfaces. No capacity of ours to
	divide — the vendor's own 429 is the only cap — so capacity stays 0 and the provider names
	itself as the deployment."""
	for model in models:
		if not model.published or model.provider not in vendors:
			continue
		vendor = vendors[model.provider]
		for engine_url, dialect in vendor["fronts"]:
			routes.setdefault(model.name, []).append({
				"engine_url": engine_url,
				"internal_key": vendor["api_key"],
				"healthy": True,
				"capacity": 0,
				"deployment": model.provider,
				"server": model.provider,
				"kind": "provider",
				"modality": modality.get(model.name, ""),
				"upstream_model": upstream.get(model.name, ""),
				"api_version": vendor["api_version"],
				"dialect": dialect,
			})


def _owned_boxes(ingress_name):
	"""{Inference Server name: private ip} for the boxes that name this ingress.

	Ownership is explicit rather than derived from the Network, which is what keeps the capacity
	gate honest: `inflight:<engine>` lives in each box's own Redis, so a replica counted by two
	ingresses is silently admitted to twice its --max-num-seqs.

	A box with no private address is left out, not dialled publicly — fail closed, rather than
	customer traffic quietly crossing the internet to a box meant to be private."""
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

	The same shape the gateway's table has, so the agent's handler is reused whole — only the
	scope narrows. An ingress is told about its own boxes and no others, so replica topology never
	leaves its own Network.

	The payload is the whole table, so anything it does not name is pruned and /pick answers 503.
	Pods are absent by construction: no Machine, so no ingress."""
	owned = _owned_boxes(ingress_name)
	routes = {}
	if not owned:
		return routes

	kinds = _engine_kinds()
	deployments = _deployments()
	# Matters MORE here than in _gateway_routes: the gateway's capacity is advisory, this one is
	# the authoritative gate. An unresolved blank would have the two planes disagreeing about one
	# engine's cap.
	replicas = [
		_resolve_deployment(replica, deployments)
		for replica in frappe.get_all(
			"Model Replica",
			filters={"status": "Active", "inference_server": ("in", list(owned))},
			fields=[
				"name", "model", "engine_url", "inference_server",
				"max_num_seqs", "model_deployment",
			],
		)
	]
	for replica in replicas:
		engine_url = private_url(replica.engine_url, owned[replica.inference_server])
		if not engine_url:
			continue
		internal_key = frappe.get_doc("Model Replica", replica.name).get_password("internal_api_key") or ""
		routes.setdefault(replica.model, []).append({
			"engine_url": engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": _capacity(replica, kinds),
			# No `server`: the ingress already holds the box's address in engine_url.
			"deployment": replica.name,
		})
	for rows in routes.values():
		rows.sort(key=lambda r: r["deployment"])
	return routes


# --- Snapshot + hash gate ----------------------------------------------------

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


def gateway_snapshot(geography):
	"""The same for every gateway in `geography`, so a run builds it once per geography. Only the
	routes differ between geographies."""
	return {
		"groups": _flat_section({"records": _effective_groups()}),
		"users": _bucketed_section(_effective_users(), "name"),
		"keys": _bucketed_section(_effective_keys(), "key_hash"),
		"routes": _flat_section({"table": _gateway_routes(geography)}),
	}


def gateway_geography(gateway):
	"""Blank for a gateway with none, which is then given no routes at all."""
	return frappe.db.get_value("Gateway Server", gateway, "geography") or ""


def ingress_snapshot(ingress):
	"""Its replica table and nothing else — that plane has no keys, users or groups section."""
	return {"routes": _flat_section({"table": _replicas_for_ingress(ingress)})}


def _delta(snapshot, remote):
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


def _describe(delta, response):
	"""Which sections went (bucket counts in brackets) and how many records the agent wrote."""
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
	"""Bring one box to the snapshot. None when it already holds it — nothing pushed, nothing to
	log. Reachability and success are separate so the log distinguishes 'box down' from 'up but
	rejected'."""
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
		# Recorded even on failure: what a rejected push tried to send is the whole question.
		"payload": _collected_payload(),
	}


# --- Sync runs ---------------------------------------------------------------

def sync_projection(trigger="Scheduled", proxies=None, ingresses=None, force=False, wait=0):
	"""Bring every target box to the current desired state. A fleet already in sync leaves no doc.

	Both kinds default to every Active box. `is None` and not truthiness: an empty list is a caller
	saying "no boxes of this kind", which is how an ingress-only run asks for no gateway work."""
	doc = _new_run("Projection", trigger)
	if not doc.acquire_lock(wait=wait):  # scheduled → skip if a run is in flight; forced → queue
		return None
	try:
		# A named gateway is pushed itself, whatever store it is on: that is an operator's button.
		groups = sync_targets() if proxies is None else [(None, [proxy]) for proxy in proxies]
		if ingresses is None:
			ingresses = [] if proxies else _active_ingresses()
		if not (groups or ingresses):
			return None

		snapshots = {}

		def snapshot_for(proxy):
			geography = gateway_geography(proxy)
			if geography not in snapshots:
				snapshots[geography] = gateway_snapshot(geography)
			return snapshots[geography]

		total = ok = 0
		stamped = False
		for store, gateways in groups:
			logged = len(doc.results)
			outcome = try_in_turn(
				doc, store, gateways,
				lambda proxy: _sync_target("Gateway Server", proxy, snapshot_for(proxy), force),
			)
			if len(doc.results) > logged:
				total, ok = total + 1, ok + (outcome is not False)
		for ingress in ingresses:
			res = _sync_target("Ingress Server", ingress, ingress_snapshot(ingress), force)
			stamped |= _stamp_synced("Ingress Server", ingress, res)
			if res is None:
				continue
			doc.append("results", {"server_type": "Ingress Server", "server": ingress, **res})
			total, ok = total + 1, ok + res["success"]

		if doc.results:
			_finalize(doc, total, ok)
			frappe.db.commit()
			return doc.name
		if stamped:
			frappe.db.commit()
		return None
	finally:
		doc.release_lock()


def check_state(server_type, name):
	"""What a tick would push right now, without pushing it. A bracketed count is how many buckets
	of that section differ."""
	if server_type == "Gateway Server":
		snapshot = gateway_snapshot(gateway_geography(name))
	else:
		snapshot = ingress_snapshot(name)
	_doc, admin_url, token = _conn(name, server_type)
	delta = _delta(snapshot, remote_hashes(admin_url, token))
	drift = [
		f"{section}[{len(content['buckets'])}]" if "buckets" in content else section
		for section, content in delta.items()
	]
	return {"in_sync": not delta, "drift": sorted(drift)}


def full_sync(proxies=None, trigger="Manual", ingresses=None, wait=60):
	"""Force-push the complete snapshot, skipping the hash gate. A button means "this box missed
	something", so it WAITS for an in-flight run rather than skipping."""
	return sync_projection(
		trigger=trigger, proxies=proxies, ingresses=ingresses, force=True, wait=wait
	)


# --- helpers ---------------------------------------------------------------

NO_WRITER = "No Active Gateway Store Writer — mark one of this store's gateways, or nothing updates it."


def sync_targets():
	"""Where a run reaches each gateway Redis, as (store, gateways) groups: a gateway on its own
	Redis alone, then each store through its Active writers in the order they are tried. A store
	whose Active gateways include no writer is a group of none."""
	alone, stores = [], {}
	gateways = frappe.get_all(
		"Gateway Server",
		filters={"status": "Active"},
		fields=["name", "gateway_store", "is_store_writer"],
		order_by="name asc",
	)
	for gateway in gateways:
		if not gateway.gateway_store:
			alone.append((None, [gateway.name]))
			continue
		writers = stores.setdefault(gateway.gateway_store, [])
		if gateway.is_store_writer:
			writers.append(gateway.name)
	return alone + sorted(stores.items())


def try_in_turn(doc, store, gateways, attempt):
	"""Run `attempt` on each gateway until one succeeds, logging a row per attempt. True when one
	succeeded, None when one already held the state (no row), False when none got through."""
	if not gateways:
		doc.append("results", {"server_type": "Gateway Store", "server": store, "error": NO_WRITER})
		return False
	for gateway in gateways:
		res = attempt(gateway)
		if res is None:
			return None
		doc.append("results", {"server_type": "Gateway Server", "server": gateway, **res})
		if res["success"]:
			return True
	return False


def _active_ingresses():
	"""One with no Network is skipped rather than thrown on: a scheduled run must not die over one
	misconfigured box."""
	return frappe.get_all(
		"Ingress Server", filters={"status": "Active", "network": ("is", "set")}, pluck="name"
	)


def _stamp_synced(doctype, name, res):
	"""Record that this box holds the desired state — on an in-sync skip, where no row is written
	and this is the only trace, and on a successful push. Never on failure: the timestamp going
	stale is what says a box has been failing."""
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
