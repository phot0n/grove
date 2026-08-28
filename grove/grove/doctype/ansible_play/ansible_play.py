# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure


class AnsiblePlay(Document):
	def on_update(self):
		"""Announce a failed play, once, wherever it came from.

		The highest-leverage place in the app to do this: run_play returns (name, 1) rather than
		raising, so whether a failed play is visible has always been the caller's choice — and six
		of them discard the code entirely, including the certificate push that runs against every
		box at midnight. Hooking the play itself covers all of them without touching any.

		update_play writes through doc.save(), which is what makes this fire at all; the status
		writes on the server docs themselves go through db.set_value and would not.

		Does not mark anything Broken. The callers that consider a failed play fatal already do,
		and the ones that deliberately do not — write_targets neither installs nor breaks an
		agent — should keep that judgement.
		"""
		if self.status != "Failure" or not self.has_value_changed("status"):
			return
		doctype = self.reference_doctype or self.server_type
		name = self.reference_docname or self.server
		failure.report(doctype, name, f"{self.playbook} failed", f"See Ansible Play {self.name}")

	@frappe.whitelist()
	def stop(self):
		"""Button: stop this play where it is. Writes the intent only — the worker running
		the playbook polls this status between tasks and on every retry of the one it is in,
		then terminates itself. A stopped play leaves the box half-configured by definition,
		so whatever it was deploying needs the cleanup below."""
		if self.status not in ("Pending", "Running"):
			frappe.throw(f"This play is {self.status} — there is nothing to stop.")
		frappe.db.set_value("Ansible Play", self.name, "status", "Stopping")
		frappe.msgprint("Stopping — the play ends once the task it is in reports back.", alert=True)

	@frappe.whitelist()
	def cleanup(self):
		"""Button: tear down what this play was deploying. Only a Model Replica play has
		per-instance state to remove — a box provision has none."""
		if self.reference_doctype != "Model Replica":
			frappe.throw("Only a Model Replica play has an instance to tear down.")
		if self.status in ("Pending", "Running", "Stopping"):
			frappe.throw(f"This play is still {self.status} — stop it before cleaning up.")
		frappe.get_doc("Model Replica", self.reference_docname).teardown()
