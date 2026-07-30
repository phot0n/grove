# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AnsiblePlay(Document):
	@frappe.whitelist()
	def stop(self):
		"""Button: stop this play where it is. Writes the intent only — the worker running
		the playbook polls this status between tasks and on every retry of the one it is in,
		then terminates itself. A stopped play leaves the box half-configured by definition,
		so whatever it was deploying needs the cleanup below."""
		if self.status not in ("Pending", "Running"):
			frappe.throw(f"This play is {self.status} — there is nothing to stop.")
		frappe.db.set_value("Ansible Play", self.name, "status", "Stopping")
		frappe.msgprint("Stopping — the play ends once the task it is in reports back.")

	@frappe.whitelist()
	def cleanup(self):
		"""Button: tear down what this play was deploying. Only a Model Deployment play has
		per-instance state to remove — a box provision has none."""
		if self.reference_doctype != "Model Deployment":
			frappe.throw("Only a Model Deployment play has an instance to tear down.")
		if self.status in ("Pending", "Running", "Stopping"):
			frappe.throw(f"This play is still {self.status} — stop it before cleaning up.")
		frappe.get_doc("Model Deployment", self.reference_docname).teardown()
