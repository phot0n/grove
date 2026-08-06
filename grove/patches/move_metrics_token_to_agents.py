import frappe


def execute():
	"""Hand the one fleet-wide metrics token down to every Monitoring Agent. The token is
	per-agent now (a regional ingestion endpoint issues its own), and an agent without one
	fails preflight — so the existing value has to land on the agents that were pushing with
	it, or monitoring stops at the next install."""
	token = frappe.utils.password.get_decrypted_password(
		"Grove Settings", "Grove Settings", "metrics_token", raise_exception=False
	)
	if not token:
		return
	for name in frappe.get_all("Monitoring Agent", pluck="name"):
		agent = frappe.get_doc("Monitoring Agent", name)
		if agent.get_password("metrics_token", raise_exception=False):
			continue
		agent.metrics_token = token
		agent.save(ignore_permissions=True)
