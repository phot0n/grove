import frappe

RENAMES = {"Grove Group Row": "Model Group Row", "Grove User Group": "Model Group"}


def execute():
	"""Before sync, or sync creates the new names as empty tables beside the old ones."""
	for old, new in RENAMES.items():
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
