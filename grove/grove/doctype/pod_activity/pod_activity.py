# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class PodActivity(Document):
	"""One lifecycle call on a Pod: what was tried, by whom, how it ended and how long it took.
	Written by PodProvisioner, never read to decide anything."""

	pass


def record(pod, event, outcome, detail="", attempt=None, started=None, trigger="Manual"):
	"""One line on a Pod, committed at once — the worker may die right after."""
	ended = frappe.utils.now_datetime()
	frappe.get_doc({
		"doctype": "Pod Activity",
		"pod": pod,
		"event": event,
		"trigger": trigger,
		"outcome": outcome,
		"attempt": attempt,
		"started": started,
		"ended": ended,
		"duration": (ended - started).total_seconds() if started else None,
		"detail": str(detail)[:10000],
	}).insert(ignore_permissions=True)
	frappe.db.commit()
