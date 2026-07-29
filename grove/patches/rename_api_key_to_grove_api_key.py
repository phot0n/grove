import frappe


def execute():
	"""Rename the API Key doctype to Grove API Key. Runs pre-model-sync so the table is
	renamed before the new doctype JSON syncs onto it — otherwise a site installed before
	the rename ends up with both doctypes and the keys stranded on the old one."""
	if frappe.db.exists("DocType", "API Key") and not frappe.db.exists("DocType", "Grove API Key"):
		frappe.rename_doc("DocType", "API Key", "Grove API Key", force=True)
