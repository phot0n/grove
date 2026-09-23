# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""deploy:<model> tables: what a gateway routes to inside its Geography, and what an ingress owns
behind it. Capacity IS the gateway's admission cap, so it resolves through the deployment in one
place — `active_replicas` — for both planes."""

import frappe

from grove.naming import short_name
from grove.net import private_url
from grove.pricing import PriceBook
from grove.serving.base import engine_class
from grove.utils import utc_today

REPLICA_FIELDS = ["name", "model", "engine_url", "inference_server", "max_num_seqs", "model_deployment"]


def ingress_targets(geography):
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


def engine_kinds():
	"""Resolved once per snapshot, not per route row: a handful of images against every placement
	in the fleet."""
	return {
		image["name"]: image["engine_kind"]
		for image in frappe.get_all("Engine Image", fields=["name", "engine_kind"])
	}


def deployments():
	"""Resolved once per snapshot, for the same reason as `engine_kinds`."""
	return {
		deployment["name"]: deployment
		for deployment in frappe.get_all(
			"Model Deployment", fields=["name", "engine_image", "max_num_seqs"]
		)
	}


def resolve_deployment(replica, by_name):
	"""A replica's effective image and admission cap. The image lives on its deployment; the cap may
	be overridden per replica, where blank means inherit.

	Not cosmetic — `max_num_seqs` IS the gateway's admission cap. A blank left unresolved would
	advertise `engine_class.default_concurrency` while the engine actually runs the deployment's
	number, and the gateway would admit against a cap the engine never had; the ingress's
	authoritative per-replica gate would then 429 traffic the gateway thought it had room for."""
	deployment = by_name.get(replica.get("model_deployment")) or {}
	replica["engine_image"] = deployment.get("engine_image")
	replica["max_num_seqs"] = replica.get("max_num_seqs") or deployment.get("max_num_seqs")
	return replica


def capacity(row, kinds):
	"""What this placement's engine runs at once. Past it the engine queues, where the gateway can
	neither see the wait nor spend it elsewhere — admission control, not a hint. No image reads as
	vllm, which is what every placement predating the field is."""
	kind = kinds.get(row.get("engine_image")) or "vllm"
	return int(row.get("max_num_seqs") or engine_class(kind).default_concurrency)


def active_replicas(**filters):
	"""Every Active Model Replica matching `filters`, its cap resolved through its deployment, and
	the engine kinds to read that cap by. → (replicas, kinds). Both planes build from this, so they
	can never disagree about one engine's cap: the gateway's is advisory, the ingress's is the gate."""
	kinds = engine_kinds()
	by_name = deployments()
	replicas = [
		resolve_deployment(replica, by_name)
		for replica in frappe.get_all(
			"Model Replica", filters={"status": "Active", **filters}, fields=REPLICA_FIELDS
		)
	]
	return replicas, kinds


def gateway_routes(geography):
	"""deploy:<model> table for every gateway in `geography`, built only from what runs inside it. A
	model with no Active engine is simply absent; the push prunes its key, so it drops out of Redis
	and /v1/models next tick."""
	if not geography:
		return {}  # a gateway outside every geography serves nothing rather than anyone's
	deps, kinds = active_replicas(geography=geography)
	# Stamped per row because deploy:<model> is the only thing pushed per model — a record of its
	# own would be a new namespace for one short string. Blank means unrestricted.
	models = frappe.get_all(
		"Model", fields=["name", "modality", "model_id", "upstream_model_id", "provider", "published"]
	)
	modality = {m.name: m.modality or "" for m in models}
	# Once, not per model: a handful of providers against thousands of models.
	vendors = vendor_endpoints(geography)
	upstream = {m.name: upstream_model(m, m.provider in vendors) for m in models}
	routes = {}
	targets = ingress_targets(geography)
	# One row per (model, ingress), so deployments behind one ingress fold together instead of
	# each getting a row naming the same URL.
	folded = {}
	for d in deps:
		# Blank resolves to the same default the serve command uses — the two have to agree or
		# the cap is not the engine's.
		cap = capacity(d, kinds)
		if d.inference_server in targets:
			target = targets[d.inference_server]
			if target is None:
				continue  # owned by an ingress that cannot take traffic — dark, not dialled direct
			# Advisory here, authoritative on the ingress: the gateway sums to choose BETWEEN
			# ingresses, and the ingress applies the exact per-replica gate. That is why two
			# gateways cannot jointly overrun a replica.
			row = folded.setdefault((d.model, target["server"]), {**target, "capacity": 0})
			row["capacity"] += cap
			continue
		internal_key = frappe.get_doc("Model Replica", d.name).get_password("internal_api_key") or ""
		routes.setdefault(d.model, []).append({
			"engine_url": d.engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": cap,
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
			"capacity": capacity(p, kinds),
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
	add_vendor_routes(routes, models, vendors, modality, upstream)
	add_rates(routes)
	for rows in routes.values():
		rows.sort(key=lambda r: r["deployment"])  # stable hash whatever the query order
	return routes


def add_rates(routes):
	"""Today's rates on every row of a priced model: sell, else the provider's cost, per counter,
	in nano-USD per unit. The gateway prices each request with these; the pull audits the sum. An
	unpriced model carries no `rates` at all, so its rows hash as before."""
	book = PriceBook.load()
	today = utc_today()
	for model, rows in routes.items():
		rates = book.rates_for(model, today)
		if rates:
			for row in rows:
				row["rates"] = rates


def upstream_model(model, is_vendor):
	"""What the upstream is asked for, or "" to send the id the caller used.

	An override always wins — the one thing that reaches a container image advertising its own
	name. A vendor otherwise gets the bare id, since our namespace is not its; anything we run
	ourselves gets "", because an engine is started under the full Grove id."""
	if model.upstream_model_id:
		return model.upstream_model_id
	return model.model_id if is_vendor else ""


def vendor_endpoints(geography):
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


def add_vendor_routes(routes, models, vendors, modality, upstream):
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


def owned_boxes(ingress_name):
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


def replicas_for_ingress(ingress_name):
	"""deploy:<model> for ONE ingress: every Active replica it owns, dialled privately.

	The same shape the gateway's table has, so the agent's handler is reused whole — only the
	scope narrows. An ingress is told about its own boxes and no others, so replica topology never
	leaves its own Network.

	The payload is the whole table, so anything it does not name is pruned and /pick answers 503.
	Pods are absent by construction: no Machine, so no ingress."""
	owned = owned_boxes(ingress_name)
	routes = {}
	if not owned:
		return routes

	replicas, kinds = active_replicas(inference_server=("in", list(owned)))
	for replica in replicas:
		engine_url = private_url(replica.engine_url, owned[replica.inference_server])
		if not engine_url:
			continue
		internal_key = frappe.get_doc("Model Replica", replica.name).get_password("internal_api_key") or ""
		routes.setdefault(replica.model, []).append({
			"engine_url": engine_url,
			"internal_key": internal_key,
			"healthy": True,
			"capacity": capacity(replica, kinds),
			# No `server`: the ingress already holds the box's address in engine_url.
			"deployment": replica.name,
		})
	for rows in routes.values():
		rows.sort(key=lambda r: r["deployment"])
	return routes
