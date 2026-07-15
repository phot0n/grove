# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import os

import frappe
from frappe.model.document import Document

from grove import gateway_sync, ansible_runner
from grove.provision import build_agent, _app_grove_root


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
		source = build_agent()
		project_dir = os.path.join(_app_grove_root(), "deploy", "gateway", "ansible")
		return ansible_runner.run_play(
			playbook="deploy_agent.yml",
			server_type="Proxy Server",
			server_name=self.name,
			machine_name=self.machine,
			project_dir=project_dir,
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

		gateway_service_source = build_agent()
		project_dir = os.path.join(_app_grove_root(), "deploy", "gateway", "ansible")

		admin_token = self.get_password("admin_token")
		play_name, rc = ansible_runner.run_play(
			playbook="proxy.yml",
			server_type="Proxy Server",
			server_name=self.name,
			machine_name=self.machine,
			project_dir=project_dir,
			extravars={"admin_token": admin_token, "agent_source": gateway_service_source},
		)

		frappe.db.set_value("Proxy Server", self.name, "status", "Active" if rc == 0 else "Broken")
		frappe.db.commit()

		if rc == 0:
			gateway_sync.full_sync(proxies=[self.name], trigger="Provision")
		return play_name, rc
