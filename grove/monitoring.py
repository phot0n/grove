# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Scrape targets Grove hands to a Monitoring Agent.

One vmagent scrapes many boxes, so nothing on a box knows what to scrape — the agent asks
here, and Grove answers out of the docs that already describe the fleet. A deploy, a new box
or a terminated pod changes the answer with no playbook to re-run and no file to write."""

import hmac
import json
from urllib.parse import urlparse

import frappe
from werkzeug.wrappers import Response

NODE_EXPORTER_PORT = 9100
DCGM_EXPORTER_PORT = 9400
# vmagent's own /metrics — its -httpListenAddr in the vmagent role.
VMAGENT_PORT = 8429


def run_exporters_play(server):
	"""Install the metrics exporters on a server's box: node, and DCGM when the Machine has
	GPU rows. Shared by Inference Server and Proxy Server — the play and the box are the same
	thing, only which doc owns the button differs.

	The exporters only listen. Which agent scrapes them is the server's `monitoring_agent`,
	and nothing installed here is told about it."""
	machine = frappe.get_doc("Machine", server.machine)
	if not machine.public_ip:
		frappe.throw(f"Machine {machine.name} has no public IP — nothing to connect to.")
	# The play lives with the agent's — it is a monitoring play whoever owns the box it lands
	# on, and the roles it needs are already there.
	return server.run_playbook(
		"exporters.yml",
		project="Monitoring Agent",
		extravars={"monitoring_has_gpu": bool(machine.gpus)},
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def targets(agent: str, kind: str = "engine", token: str = ""):
	"""Prometheus http_sd list for one Monitoring Agent.

	kind='host'   → the node and GPU exporters on every box that names this agent.
	kind='engine' → the vLLM engines on those boxes, plus every pod that names it.

	Two kinds because they are scraped at different intervals. A raw Response, not a return
	value: http_sd wants a bare JSON array, and Frappe would otherwise wrap it in its
	{"message": ...} envelope, which vmagent rejects.

	Guest-whitelisted and authenticated on `token` instead: Frappe's validate_auth rejects any
	Authorization header it cannot resolve to a user before a whitelisted method ever runs, so
	an agent carrying a bare shared secret can only reach here through the query string."""
	authenticate(token)
	entries = host_targets(agent) if kind == "host" else engine_targets(agent)
	return Response(json.dumps(entries), mimetype="application/json")


def authenticate(token):
	"""The Service Discovery Token from Grove Settings, compared in constant time.

	This is the whole of what stands between the internet and the fleet's inventory — every
	box's address and open exporter ports, every model and engine URL — so an unset token
	refuses rather than waving everyone through.

	A signed-in user who may read the agent skips it: that is the form's Show Targets button,
	and they can already read every doc this list is built from. Putting the shared secret in a
	browser URL to satisfy a check they have already passed would only spread it around."""
	if frappe.session.user != "Guest" and frappe.has_permission("Monitoring Agent", "read"):
		return

	expected = frappe.get_cached_doc("Grove Settings").get_password("sd_token", raise_exception=False)
	if not expected:
		frappe.throw(
			"No Service Discovery Token is set in Grove Settings.", frappe.AuthenticationError
		)
	if not hmac.compare_digest(str(token), str(expected)):
		frappe.throw("Invalid Service Discovery Token.", frappe.AuthenticationError)


def host_targets(agent):
	"""Exporter targets for every box this agent owns, including its own."""
	return build_host_targets(inference_boxes(agent) + proxy_boxes(agent)) + agent_targets(agent)


def agent_targets(agent):
	"""The agent's own box. Nothing else names it — it carries no Inference or Proxy Server
	doc — and an agent that cannot see its own disk filling is the blind spot that matters:
	vmagent's queue depth and dropped-sample counters are how a broken pipeline announces
	itself, before anyone notices a gap in a dashboard.

	Scraped over localhost, since this is the box doing the scraping — nothing here has to be
	reachable from outside, and no security group rule is needed for it."""
	box = frappe.db.get_value("Monitoring Agent", agent, ["machine", "region"], as_dict=True)
	if not box:
		return []
	labels = {"machine": box.machine or "", "region": box.region or "", "server": agent}
	return [
		exporter_entry("127.0.0.1", NODE_EXPORTER_PORT, labels),
		exporter_entry("127.0.0.1", VMAGENT_PORT, labels),
	]


def engine_targets(agent):
	"""Engines this agent scrapes: every Active deployment on its boxes, and every Running
	pod that names it. Both are reached at their engine_url — the address the gateway
	already routes to."""
	boxes = {box["name"]: box for box in inference_boxes(agent)}
	deployments = (
		frappe.get_all(
			"Model Deployment",
			filters={"inference_server": ("in", list(boxes)), "status": "Active"},
			fields=["name", "model", "engine_url", "inference_server"],
		)
		if boxes
		else []
	)
	entries = [
		engine_entry(
			deployment.engine_url,
			{
				"model": deployment.model,
				"deployment": deployment.name,
				"server": deployment.inference_server,
				"machine": boxes[deployment.inference_server]["machine"],
				"region": boxes[deployment.inference_server]["region"],
			},
		)
		for deployment in deployments
	]
	# Pods carry no machine/region — they are not on a box we own.
	entries += [
		engine_entry(pod.engine_url, {"model": pod.model, "deployment": pod.name})
		for pod in frappe.get_all(
			"Pod",
			filters={"monitoring_agent": agent, "status": "Running"},
			fields=["name", "model", "engine_url"],
		)
	]
	return [entry for entry in entries if entry]


def inference_boxes(agent):
	"""Inference Servers this agent scrapes, as plain rows: name, box, address, GPUs."""
	servers = frappe.get_all(
		"Inference Server",
		filters={"monitoring_agent": agent},
		fields=["name", "machine", "machine_ip as ip", "region"],
	)
	return _with_gpu_flag(servers)


def proxy_boxes(agent):
	"""Proxy Servers this agent scrapes. Never GPU boxes — no DCGM target for them."""
	servers = frappe.get_all(
		"Proxy Server",
		filters={"monitoring_agent": agent},
		fields=["name", "machine", "public_ip as ip", "region"],
	)
	return [{**server, "has_gpu": False} for server in servers]


def _with_gpu_flag(servers):
	"""Whether each box has GPUs, from the Machine's own scanned rows rather than a flag
	somebody has to remember to set."""
	gpu_machines = set(frappe.get_all("Machine GPU", pluck="parent"))
	return [{**server, "has_gpu": server["machine"] in gpu_machines} for server in servers]


def build_host_targets(boxes):
	"""Boxes → exporter entries. A box with no address cannot be scraped — it is skipped
	rather than emitted as a target that can only ever be down."""
	entries = []
	for box in boxes:
		if not box.get("ip"):
			continue
		labels = {
			"machine": box.get("machine") or "",
			"region": box.get("region") or "",
			"server": box["name"],
		}
		entries.append(exporter_entry(box["ip"], NODE_EXPORTER_PORT, labels))
		if box.get("has_gpu"):
			entries.append(exporter_entry(box["ip"], DCGM_EXPORTER_PORT, labels))
	return entries


def exporter_entry(ip, port, labels):
	return {"targets": [f"{ip}:{port}"], "labels": dict(labels)}


def engine_entry(engine_url, labels):
	"""One entry for an engine, addressed exactly where the gateway routes it. None when
	there is no URL yet — a deployment mid-provision, or a pod still loading."""
	parsed = urlparse(engine_url or "")
	if not parsed.hostname:
		return None
	port = parsed.port or (443 if parsed.scheme == "https" else 80)
	return {
		"targets": [f"{parsed.hostname}:{port}"],
		"labels": {
			# Verbatim: the exact string gateway_sync pushes as this engine's route target.
			# Reformatted, the series can no longer be joined to the route it describes.
			"engine": engine_url,
			"__scheme__": parsed.scheme or "http",
			**{key: value or "" for key, value in labels.items()},
		},
	}
