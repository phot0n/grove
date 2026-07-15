# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GroveSettings(Document):
	@frappe.whitelist()
	def full_sync_all(self):
		"""Button: push the COMPLETE state to every Active proxy."""
		frappe.enqueue("grove.gateway_sync.full_sync", queue="short", trigger="Manual")
		frappe.msgprint("Full sync queued for all Active proxies.")
