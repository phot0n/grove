import frappe


def execute():
	"""Teardown used to leave a Model Deployment Inactive. It writes Terminated now, and
	Inactive means something else — a Stop that left the container on the box, ready to Start.
	Rows torn down before that split would otherwise claim a port and GPUs nothing holds."""
	frappe.db.set_value(
		"Model Deployment", {"status": "Inactive"}, "status", "Terminated", update_modified=False
	)
