import frappe


def execute():
	"""Rename the Proxy Server doctype to Gateway Server.

	The doc runs auth, quota, usage and routing — it is the gateway, and everything around it
	already said so (gateway_service, gateway_sync, GROVE_GATEWAY_ID, Grove Settings.gateway_host).
	The doctype was the last holdout, and once an Ingress Server exists "proxy" is actively wrong:
	the ingress is the thing that is actually a proxy.

	Pre-model-sync, like the Grove API Key rename before it, so the table is renamed before the
	new doctype JSON syncs onto it — otherwise a site installed before this ends up with both
	doctypes and every proxy stranded on the old one.

	Doc NAMES are untouched, so Route53 record sets (keyed on the doc name as SetIdentifier),
	admin_url and hostname all keep pointing where they did."""
	if frappe.db.exists("DocType", "Proxy Server") and not frappe.db.exists(
		"DocType", "Gateway Server"
	):
		frappe.rename_doc("DocType", "Proxy Server", "Gateway Server", force=True)
