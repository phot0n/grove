import frappe

# Ansible's own words, which Ansible Task used to store verbatim → the statuses it stores now.
OLD_STATUS = {
	"ok": "Success",
	"changed": "Success",
	"failed": "Failure",
	"unreachable": "Unreachable",
	"skipped": "Skipped",
}


def execute():
	"""Restate historic Ansible Tasks in the new status vocabulary. Left alone they read as a
	blank Select — every past play would show its tasks as having no status at all."""
	for old, new in OLD_STATUS.items():
		frappe.db.set_value("Ansible Task", {"status": old}, "status", new, update_modified=False)
