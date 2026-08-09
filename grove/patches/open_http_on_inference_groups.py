import frappe


def execute():
	"""Open port 80 on every inference security group, to the same sources 443 already allows.

	The box's nginx is moving off 443, and a box whose group has not caught up would be
	unreachable the moment it does. Opening 80 first costs nothing while nothing listens on it.

	Inline rather than through sync_fleet_ingress, which enqueues: a migrate that returned before
	AWS had the rules would leave that ordering to luck.

	A Network that cannot be reconciled is logged and skipped — one account's expired credentials
	must not stop the migration for every other."""
	networks = frappe.get_all(
		"Network", filters={"inference_security_group_ids": ["is", "set"]}, pluck="name"
	)
	for name in networks:
		try:
			frappe.get_doc("Network", name).sync_inference_ingress()
		except Exception:
			frappe.log_error(title=f"Port 80 not opened on Network {name}")
