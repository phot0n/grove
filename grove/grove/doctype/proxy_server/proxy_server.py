# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import gateway_sync
from grove.ansible import Ansible
from grove.monitoring import run_exporters_play
from grove.utils import ansible_project_dir, gateway_service_source


class ProxyServer(Document):
	def on_update(self):
		# A newly-Active proxy needs the full current state now — the background
		# job only pushes dirty deltas, so full-sync this one immediately.
		if self.has_value_changed("status") and self.status == "Active" and self.admin_url:
			frappe.enqueue(
				"grove.gateway_sync.full_sync",
				queue="short",
				proxies=[self.name],
				trigger="Proxy Activated",
			)

	@frappe.whitelist()
	def full_sync(self):
		"""Button: push the COMPLETE key set + routing table to this proxy now
		(logged on a Gateway Sync doc)."""
		frappe.enqueue(
			"grove.gateway_sync.full_sync",
			queue="short",
			proxies=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Full sync queued for {self.name}.")

	@frappe.whitelist()
	def install_exporters(self):
		"""Button: install this box's metrics exporters (long job — it SSHes to the box).
		They only listen; the Monitoring Agent named on this doc is what scrapes them."""
		if not self.machine:
			frappe.throw("Set a Machine before installing exporters.")
		frappe.enqueue_doc(
			self.doctype, self.name, "provision_exporters", queue="long", timeout=1800
		)
		frappe.msgprint(f"Installing the metrics exporters on {self.name} — watch its Ansible Plays.")

	def provision_exporters(self):
		return run_exporters_play(self)

	@frappe.whitelist()
	def deploy_agent(self):
		"""Button: build the latest gateway binary and deploy just it (copy +
		service restart) to this already-provisioned proxy."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_deploy_agent",
			queue="long",
			timeout=1200,
		)
		frappe.msgprint(f"Building + deploying latest agent to {self.name} — watch its Ansible Plays.")

	def _deploy_agent(self):
		"""Push gateway_service source to Proxy Server, build + restart (no
		OpenResty/Redis reinstall)."""
		source = gateway_service_source()
		project_dir = ansible_project_dir("gateway")
		ansible = Ansible(project_root=project_dir)
		return ansible.run_playbook(
			playbook_name="deploy_agent.yml",
			server_type="Proxy Server",
			server_name=self.name,
			machine_name=self.machine,
			extravars={"agent_source": source},
		)

	@frappe.whitelist()
	def setup(self):
		"""Provision this proxy (OpenResty + Redis + Go agent) via proxy.yml."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"provision",
			queue="long",
			timeout=1800,
		)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.")

	def provision(self):
		"""Run proxy.yml against the Proxy Server's Machine → OpenResty + Redis +
		Go agent. On success, mark Active and project keys/routes."""
		frappe.db.set_value("Proxy Server", self.name, "status", "Installing")
		frappe.db.commit()

		source = gateway_service_source()
		project_dir = ansible_project_dir("gateway")
		ansible = Ansible(project_root=project_dir)

		admin_token = self.get_password("admin_token")
		play_name, rc = ansible.run_playbook(
			playbook_name="proxy.yml",
			server_type="Proxy Server",
			server_name=self.name,
			machine_name=self.machine,
			extravars={"admin_token": admin_token, "agent_source": source, "gateway_id": self.name},
		)

		frappe.db.set_value("Proxy Server", self.name, "status", "Active" if rc == 0 else "Broken")
		frappe.db.commit()

		if rc == 0:
			gateway_sync.full_sync(proxies=[self.name], trigger="Provision")
		return play_name, rc
