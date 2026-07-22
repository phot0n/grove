# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class Machine(Document):
	@frappe.whitelist()
	def setup(self):
		"""Provision this machine. If cloud_provider is set, calls cloud provider API
		to spawn the machine, then enqueues Ansible provisioning (bootstrap + inference-host).
		If cloud_provider is blank, assumes manual/on-prem and enqueues Ansible directly."""
		if self.cloud_provider:
			# Cloud provisioning: spawn pod/instance, get IP, then Ansible
			frappe.enqueue(
				"grove.cloud_provider.provisioner.provision_machine",
				queue="long",
				timeout=1800,
				machine_name=self.name,
			)
			frappe.msgprint(f"Cloud provisioning started for {self.name} on {self.cloud_provider}.")
		else:
			# On-prem/manual: assume IP is already set, enqueue Ansible directly
			frappe.enqueue(
				"grove.provision.provision_machine_ansible",
				queue="long",
				timeout=1800,
				machine_name=self.name,
			)
			frappe.msgprint(f"Provisioning {self.name} (manual/on-prem) — watch its Ansible Plays.")

	@frappe.whitelist()
	def teardown(self):
		"""Terminate the cloud pod backing this Machine (frees GPU/disk/billing) and mark
		it Offline. Cloud only — no-op for on-prem."""
		if not self.cloud_provider:
			frappe.throw("Machine has no cloud_provider — nothing to terminate.")
		frappe.enqueue(
			"grove.cloud_provider.provisioner.deprovision_machine",
			queue="long",
			timeout=600,
			machine_name=self.name,
		)
		frappe.msgprint(f"Terminating pod for {self.name} on {self.cloud_provider}.")

	@frappe.whitelist()
	def recover(self):
		"""Re-read the pod's endpoints after a restart (IP/ports may move; a RunPod
		restart also wipes the ephemeral root) and redeploy each Active model onto it.
		Cloud only."""
		if not self.cloud_provider:
			frappe.throw("Machine has no cloud_provider — nothing to recover.")
		frappe.enqueue(
			"grove.cloud_provider.provisioner.recover_machine",
			queue="long",
			timeout=3600,
			machine_name=self.name,
		)
		frappe.msgprint(f"Recovering {self.name} — re-reading endpoints and redeploying models.")
