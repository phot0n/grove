import frappe
from frappe.model.naming import make_autoname


def execute():
	"""Grove User is hash-named now (title_field = user), so Grove API Key and Usage Record
	can link to it instead of carrying the email themselves. Existing docs are still named by
	email: rename them, which is what moves the links — frappe.rename_doc rewrites every Link
	field pointing at the doctype."""
	for name, email in frappe.get_all("Grove User", fields=["name", "user"], as_list=True):
		if name == email:
			frappe.rename_doc(
				"Grove User", name, make_autoname("hash", "Grove User"),
				force=True, show_alert=False, rebuild_search=False,
			)

	_adopt_orphan_keys()


def _adopt_orphan_keys():
	"""A key minted before Grove User existed still holds an email in `user`, which now points
	nowhere. Give each one a policy doc (blank = fail closed, no group and no allow) and
	repoint the key and its usage records at it. Keys whose User is gone are left alone —
	they were already dangling."""
	known = set(frappe.get_all("Grove User", pluck="name"))
	orphans = {
		k.user for k in frappe.get_all("Grove API Key", fields=["user"])
		if k.user and k.user not in known
	}

	for email in orphans:
		if not frappe.db.exists("User", email):
			continue
		name = frappe.db.get_value("Grove User", {"user": email})
		if not name:
			doc = frappe.new_doc("Grove User")
			doc.user = email
			doc.insert(ignore_permissions=True)
			name = doc.name
		frappe.db.set_value("Grove API Key", {"user": email}, "user", name, update_modified=False)
		frappe.db.set_value("Usage Record", {"user": email}, "user", name, update_modified=False)
