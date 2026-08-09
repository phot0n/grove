import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	"""Carry the data that still says "Proxy Server" across to the new name.

	Post-model-sync, because rename_field needs the NEW fieldname present in the doctype meta
	before it will move anything onto it. Each step is guarded on the old value still being there,
	so a re-run is a no-op rather than an error."""
	# Billing-adjacent: it attributes usage to the gateway that served it, so it is moved rather
	# than left to be recreated empty beside a populated column nothing reads.
	frappe.reload_doc("grove", "doctype", "usage_gateway_row")
	if "proxy_server" in frappe.db.get_table_columns("Usage Gateway Row"):
		rename_field("Usage Gateway Row", "proxy_server", "gateway_server")

	# Grove Settings is a Single, so this value lives as a row in tabSingles, not a column. Checked
	# with raw SQL, not frappe.db.exists: tabSingles has no `name` column for exists() to select,
	# so it answers falsy for a row that is right there and the rename skips without a word.
	frappe.reload_doc("grove", "doctype", "grove_settings")
	if frappe.db.sql(
		"""SELECT 1 FROM `tabSingles` WHERE doctype = 'Grove Settings' AND field = 'proxy_zone' LIMIT 1"""
	):
		rename_field("Grove Settings", "proxy_zone", "fleet_zone")

	# Machine Type is a Select, so the old label is stored as data on every existing gateway box.
	# Left behind it fails silently and late: get_security_group_ids matches on the literal
	# "Gateway Server" (machine.py), so a box still reading "Proxy Server" falls through to the
	# INFERENCE group list and comes back from its next launch with 80 and 443 shut.
	frappe.db.sql(
		"""UPDATE `tabMachine` SET machine_type = 'Gateway Server' WHERE machine_type = 'Proxy Server'"""
	)
