# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.utils import ansible_project_dir


class InferenceServer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		is_provisioned: DF.Check
		machine: DF.Link
		machine_ip: DF.Data | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken"]
	# end: auto-generated types

	@frappe.whitelist()
	def get_gpu_allocation(self):
		"""The box's GPUs and which deployments hold them, computed live: the cards come
		from the Machine, the claims from every Active Model Deployment on this server.
		Nothing is stored, so it can't drift out of step with what's actually running.

		Two deployments naming the same CUDA index is not prevented anywhere — the row
		reports every claimant so the clash is visible rather than silently halving VRAM."""
		if not self.machine:
			return []
		gpus = frappe.get_all(
			"Machine GPU",
			filters={"parent": self.machine, "parenttype": "Machine"},
			fields=["gpu_index", "gpu_model", "vram_gb"],
			order_by="gpu_index",
		)
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
			timeout=3600,
		)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.")

	def provision(self):
		"""One-time host bootstrap for an Inference Server: NVIDIA driver + data
		volume (the gpu_host role via provision.yml). Runs once per box — model
		serves (deploy_model) assume an already-provisioned host and gate on
		is_provisioned. Mirrors deploy_agent on the proxy side."""
		frappe.db.set_value("Inference Server", self.name, "status", "Installing")
		frappe.db.commit()

		project_dir = ansible_project_dir("vllm")
		ansible = Ansible(project_root=project_dir)
		play_name, rc = ansible.run_playbook(
			playbook_name="provision.yml",
			server_type="Inference Server",
			server_name=self.name,
			machine_name=self.machine,
		)

		ok = rc == 0
		frappe.db.set_value(
			"Inference Server",
			self.name,
			{"status": "Active" if ok else "Broken", "is_provisioned": 1 if ok else 0},
		)
		return play_name, rc
