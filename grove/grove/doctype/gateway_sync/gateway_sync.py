# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GatewaySync(Document):
	"""One row per sync run (a log). The work lives in grove.gateway_sync
	(full_sync / sync_dirty) and grove.usage_pull, which build these docs, take
	the doc's lock, run, then insert + finalize.

	The advisory lock lives here and is named by `sync_type`, so runs of the same
	type serialize (a slow one can't land a stale write after a newer one) while
	different types (Projection vs Usage) run independently."""

	def lock_name(self):
		# GET_LOCK is server-global.
		return f"grove_gateway_sync:{self.sync_type}:{frappe.local.site}"

	def acquire_lock(self, wait=0):
		"""Try to take this sync_type's lock. wait=0 → non-blocking (scheduled
		runs skip if one is in flight); wait>0 → block up to N seconds (forced
		runs queue behind the current one)."""
		return frappe.db.sql("SELECT GET_LOCK(%s, %s)", (self.lock_name(), wait))[0][0] == 1

	def release_lock(self):
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (self.lock_name(),))
