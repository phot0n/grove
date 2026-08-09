# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, now_datetime

# How long a sync run is worth keeping. Log Settings owns the number once an operator edits it
# there; this is the default the hook seeds it with.
RETENTION_DAYS = 60


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

	@staticmethod
	def clear_old_logs(days=RETENTION_DAYS):
		"""Drop runs older than `days`. Frappe's Log Settings calls this nightly; the signature is
		its LogType protocol, which is also what puts Gateway Sync in that form's list.

		This table grows on a timer rather than on use — the scheduled run writes one doc every two
		minutes whether or not anything moved, which is around 22,000 docs a quarter, each with a
		row per box and now a payload on each row. Two months is long enough to answer "what did
		the fleet do last week" and short enough that the answer stays fast.

		The child rows go first and by join, not by collecting parent names into an IN list: at
		this size that list is tens of thousands of ids, and a delete that has to be handed every
		one of them is the kind that gets killed halfway and leaves the table half cleared."""
		cutoff = add_days(now_datetime(), -cint(days))
		frappe.db.sql(
			"""DELETE FROM `tabGateway Sync Row`
			WHERE parenttype = 'Gateway Sync'
			  AND parent IN (SELECT name FROM `tabGateway Sync` WHERE creation < %s)""",
			(cutoff,),
		)
		frappe.db.delete("Gateway Sync", {"creation": ("<", cutoff)})
