# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
import frappe
from frappe.model.document import Document


class AnsibleTask(Document):
	def on_update(self):
		frappe.publish_realtime(
			"ansible_play_update",
			doctype="Ansible Play",
			docname=self.play,
			message={"id": self.play},
		)
