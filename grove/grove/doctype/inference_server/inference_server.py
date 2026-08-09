# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document

from grove import gateway_sync
from grove.ansible import AnsibleHost
from grove.monitoring import run_exporters_play
from grove.naming import GeneratedName
from grove.utils import validate_id_safe_name


class InferenceServer(GeneratedName, AnsibleHost, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		data_path: DF.Data
		ingress: DF.Link | None
		is_provisioned: DF.Check
		is_static_ip: DF.Check
		machine: DF.Link
		machine_ip: DF.Data | None
		monitoring_agent: DF.Link | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	# Its name is no DNS record of its own, but it rides on every route as `server` and into request ids.
	name_prefix = "inf"

	def validate(self):
		self.validate_ingress_network()

	def on_update(self):
		"""Moving a box between ingresses is the cutover, and it changes three tables at once:
		the old owner loses these replicas, the new one gains them, and every gateway's row for
		these models flips between a direct engine and an ingress hand-off.

		Pushed here because nothing else would. The scheduled run repairs it within a tick, but a
		cutover that only takes effect on the next cron is a cutover nobody can watch."""
		if not self.has_value_changed("ingress"):
			return
		before = self.get_doc_before_save()
		moved = [self.ingress, before.ingress if before else None]
		frappe.enqueue(
			"grove.gateway_sync.full_sync",
			queue="short",
			trigger="Provision",
			ingresses=gateway_sync.active_among(moved),
		)

	def validate_ingress_network(self):
		"""An ingress can only reach this box privately if the two share a VPC.

		Checked here because nothing downstream can say so: _replicas_for_ingress selects on this
		link, so a mismatch produces an ingress with an empty table and a model that reads
		unavailable — a silence, days after the save, with nothing pointing back at this field."""
		if not self.ingress:
			return
		ingress_network = frappe.db.get_value("Ingress Server", self.ingress, "network")
		box_network = frappe.db.get_value("Machine", self.machine, "network") if self.machine else None
		if ingress_network != box_network:
			frappe.throw(
				f"Ingress Server {self.ingress} is in Network {ingress_network or 'none'}, but "
				f"{self.name} is in {box_network or 'none'}. An ingress reaches only the boxes "
				f"inside its own VPC."
			)

	def before_insert(self):
		# Generated names are id-safe by construction, but a Region named with a dot in it would
		# slug into one that is not — so the check stays, here and on rename.
		validate_id_safe_name(self.doctype, self.name)

	def before_rename(self, old_name, new_name, merge=False):
		validate_id_safe_name(self.doctype, new_name)

	# ── The box ───────────────────────────────────────────────────────────────
	# Everything that reaches the hardware goes through here: a Model Deployment talks to
	# its Inference Server, and the Server is the only side that knows about a Machine.

	@property
	def machine_doc(self):
		"""The box this server runs on."""
		if not self.machine:
			frappe.throw(f"Inference Server {self.name} has no Machine to reach.")
		return frappe.get_doc("Machine", self.machine)

	@property
	def gpus(self):
		"""The cards on this box (Machine GPU rows), in CUDA index order."""
		if not self.machine:
			return []
		return frappe.get_all(
			"Machine GPU",
			filters={"parent": self.machine, "parenttype": "Machine"},
			fields=["gpu_index", "gpu_model", "vram_gb"],
			order_by="gpu_index",
		)

	def run_command(self, command, timeout=60):
		"""Run one argv on this server's box over SSH and return what it printed."""
		return self.machine_doc.run_command(command, timeout=timeout)

	def stream_command(self, command):
		"""Follow one argv on this server's box, yielding its output line by line."""
		return self.machine_doc.stream_command(command)

	@frappe.whitelist()
	def get_gpu_allocation(self):
		"""The box's GPUs and which deployments hold them, computed live: the cards come
		from the Machine, the claims from every Active Model Deployment on this server.
		Nothing is stored, so it can't drift out of step with what's actually running.

		Two deployments naming the same CUDA index is not prevented anywhere — the row
		reports every claimant so the clash is visible rather than silently halving VRAM."""
		gpus = self.gpus
		claims = {}
		for deployment in frappe.get_all(
			"Model Deployment",
			filters={"inference_server": self.name, "status": "Active"},
			fields=["name", "model"],
		):
			for row in frappe.get_all(
				"Model Deployment GPU",
				filters={"parent": deployment.name, "parenttype": "Model Deployment"},
				fields=["gpu_index"],
			):
				claims.setdefault(row.gpu_index, []).append(deployment)
		for gpu in gpus:
			holders = claims.get(gpu.gpu_index, [])
			gpu.deployments = holders
			gpu.status = ("Allocated" if len(holders) == 1 else "Conflict") if holders else "Free"
		return gpus

	@frappe.whitelist()
	def install_exporters(self):
		"""Button: install this box's metrics exporters — node, and DCGM since it has GPUs
		(long job — it SSHes to the box). They only listen; the Monitoring Agent named on
		this doc is what scrapes them."""
		if not self.machine:
			frappe.throw("Set a Machine before installing exporters.")
		frappe.enqueue_doc(
			self.doctype, self.name, "provision_exporters", queue="long", timeout=1800
		)
		frappe.msgprint(f"Installing the metrics exporters on {self.name} — watch its Ansible Plays.")

	def provision_exporters(self):
		return run_exporters_play(self)

	@frappe.whitelist()
	def setup(self):
		"""Button: one-time host bootstrap (NVIDIA driver + data volume) via the
		gpu_host role. Run once per box before deploying models onto it — Model
		Deployment.setup gates on is_provisioned."""
		if not self.machine:
			frappe.throw("Set a Machine before provisioning.")
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"provision",
			queue="long",
			# Has to outlast the driver reboot, which is half an hour on a bare metal box.
			timeout=5400,
		)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.")

	def provision(self):
		"""One-time host bootstrap for an Inference Server: NVIDIA driver + data
		volume + Docker (the gpu_host role), then its metrics exporters (node_exporter,
		dcgm_exporter if the Machine has GPUs) — all in the one provision.yml play, so Setup
		is a single Ansible run. Runs once per box — model serves (deploy_model) assume an
		already-provisioned host and gate on is_provisioned. Mirrors deploy_agent on the
		proxy side."""
		frappe.db.set_value("Inference Server", self.name, "status", "Installing")
		frappe.db.commit()

		is_bare_metal = frappe.db.get_value("Machine", self.machine, "is_bare_metal")
		play_name, rc = self.run_playbook(
			"provision.yml",
			extravars={
				"gpu_data_mount": self.data_path,
				"monitoring_has_gpu": bool(self.gpus),
				# The driver reboot outlasts Ansible's default on a bare metal box.
				"gpu_reboot_timeout": 1800 if is_bare_metal else 600,
				# The engine proxy's htpasswd: this play and serve.yml both write it, from the
				# one source, so whichever runs last cannot disagree with the other.
				**frappe.get_single("Grove Settings").scrape_auth_variables,
			},
		)

		ok = rc == 0
		frappe.db.set_value(
			"Inference Server",
			self.name,
			{"status": "Active" if ok else "Broken", "is_provisioned": 1 if ok else 0},
		)
		return play_name, rc
