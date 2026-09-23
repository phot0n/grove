import frappe
from frappe.model.document import Document


class LostUsage(Document):
	"""Usage a gateway already deleted that Grove could not record: one payload per row, kept
	until the hourly replay lands it through the normal pull path. Never deleted."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Data | None
		attempts: DF.Int
		day: DF.Date | None
		gateway_server: DF.Data | None
		grove_user: DF.Data | None
		last_error: DF.Code | None
		payload: DF.JSON | None
		redis: DF.Data | None
		replayed: DF.Check
		replayed_on: DF.Datetime | None
	# end: auto-generated types

	@frappe.whitelist()
	def replay(self):
		"""Land the payload on the day it was drained. A key that fails again gets a row of its
		own from the pull path, so this row is done either way; a replay that fails outright
		stays pending with its error. → landed?"""
		from grove.pathway.usage import record_usages

		frappe.only_for("System Manager")
		attempts = (self.attempts or 0) + 1
		try:
			record_usages(self.gateway_server, frappe.parse_json(self.payload), redis=self.redis or None, day=self.day)
		except Exception:
			frappe.db.rollback()
			self.db_set({"attempts": attempts, "last_error": frappe.get_traceback()}, update_modified=False)
			frappe.db.commit()
			return False
		self.db_set({"replayed": 1, "replayed_on": frappe.utils.now(), "attempts": attempts}, update_modified=False)
		frappe.db.commit()
		return True


def record_lost(gateway_server, redis, day, usages, api_key=None, grove_user=None):
	"""Called inside the except that lost it: the traceback is the reason. Plain Data fields and
	`ignore_permissions`, because this write must not be the second failure."""
	return frappe.get_doc({
		"doctype": "Lost Usage", "gateway_server": gateway_server, "redis": redis, "day": day,
		"api_key": api_key, "grove_user": grove_user, "payload": usages, "last_error": frappe.get_traceback(),
	}).insert(ignore_permissions=True).name


def replay_pending():
	"""Hourly: every row not yet landed, oldest first. → the names that landed."""
	pending = frappe.get_all("Lost Usage", filters={"replayed": 0}, pluck="name", order_by="creation asc")
	return [name for name in pending if frappe.get_doc("Lost Usage", name).replay()]
