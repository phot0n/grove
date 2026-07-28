import frappe


def execute():
	"""Membership moves off the Grove User Group `members` child table onto a `user_group`
	link on Grove User — one owner for the edge, edited where the rest of the user's policy
	already lives.

	A link holds one group, so a user who was in several keeps the first and has the other
	groups' models folded into their own Allow list. Nobody loses access; an admin can tidy
	up afterwards."""
	if not frappe.db.table_exists("Grove User Row"):
		return

	for user, groups in _memberships().items():
		if not frappe.db.exists("User", user):
			continue
		_move(user, groups[0], _models_of(groups[1:]))

	frappe.delete_doc("DocType", "Grove User Row", ignore_missing=True, force=True)
	frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabGrove User Row`")


def _memberships():
	"""{user: [group, ...]} off the child table, stable order so the kept group is."""
	memberships = {}
	rows = frappe.db.sql(
		"""SELECT user, parent FROM `tabGrove User Row`
		WHERE parenttype = 'Grove User Group' ORDER BY parent, idx""",
		as_dict=True,
	)
	for row in rows:
		if row.user and frappe.db.exists("Grove User Group", row.parent):
			memberships.setdefault(row.user, []).append(row.parent)
	return memberships


def _models_of(groups):
	if not groups:
		return []
	return frappe.get_all(
		"Grove Model Row",
		filters={"parenttype": "Grove User Group", "parent": ("in", groups)},
		pluck="model",
	)


def _move(user, group, extra_models):
	name = frappe.db.get_value("Grove User", {"user": user})
	doc = frappe.get_doc("Grove User", name) if name else frappe.new_doc("Grove User")
	doc.user = user
	doc.user_group = group

	# Deny still wins, so anything already denied must not come back in as an Allow.
	denied = {row.model for row in doc.deny}
	allowed = {row.model for row in doc.allow}
	for model in extra_models:
		if model not in allowed and model not in denied:
			doc.append("allow", {"model": model})
			allowed.add(model)
	doc.save(ignore_permissions=True)
