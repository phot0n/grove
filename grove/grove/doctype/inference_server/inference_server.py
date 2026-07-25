# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.utils import ansible_project_dir


class InferenceServer(Document):
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
