# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, now_datetime

# Log Settings owns the number once an operator edits it there; this seeds it.
RETENTION_DAYS = 60


class PathwaySync(Document):
	"""One row per sync run. The work lives in grove.pathway (projection, usage), which builds
	these docs, take the lock, run, then insert and finalize.

	The advisory lock is named by `sync_type`, so runs of the same type serialize — a slow one
	cannot land a stale write after a newer one — while Projection and Usage run independently."""

	def lock_name(self):
		# GET_LOCK is server-global, hence the site in the name.
		return f"grove_pathway_sync:{self.sync_type}:{frappe.local.site}"

	def acquire_lock(self, wait=0):
		"""wait=0 → non-blocking, so a scheduled run skips if one is in flight. wait>0 → block up
		to N seconds, so a forced run queues behind it."""
		return frappe.db.sql("SELECT GET_LOCK(%s, %s)", (self.lock_name(), wait))[0][0] == 1

	def release_lock(self):
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (self.lock_name(),))

	@staticmethod
	def clear_old_logs(days=RETENTION_DAYS):
		cutoff = add_days(now_datetime(), -cint(days))
		frappe.db.sql(
			"""DELETE FROM `tabPathway Sync Row`
			WHERE parenttype = 'Pathway Sync'
			  AND parent IN (SELECT name FROM `tabPathway Sync` WHERE creation < %s)""",
			(cutoff,),
		)
		frappe.db.delete("Pathway Sync", {"creation": ("<", cutoff)})


@frappe.whitelist(methods=["POST"])
def force_sync_all():
	frappe.only_for("System Manager")
	frappe.enqueue("grove.pathway.projection.full_sync", queue="short", trigger="Manual")
	frappe.msgprint("Force sync queued for every Active box.", alert=True)
