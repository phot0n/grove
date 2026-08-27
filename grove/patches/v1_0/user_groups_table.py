# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe


def execute():
	"""Membership moves off the `user_group` Link onto the `user_groups` child table, so a user
	can belong to several groups and reach the union of what they grant.

	Reads the old column with raw SQL: this runs post_model_sync, where the docfield is already
	gone from the meta. Frappe never drops the column, so it is also what makes the patch a no-op
	on a fresh site — but not what makes it idempotent, since it stays behind forever."""
	if "user_group" not in frappe.db.get_table_columns("Grove User"):
		return

	held = set(
		frappe.get_all("Grove Group Row", filters={"parenttype": "Grove User"}, pluck="parent")
	)
	rows = frappe.db.sql(
		"select name, user_group from `tabGrove User` where ifnull(user_group, '') != ''",
		as_dict=True,
	)
	for row in rows:
		if row.name in held:
			continue
		frappe.get_doc(
			{
				"doctype": "Grove Group Row",
				"parent": row.name,
				"parenttype": "Grove User",
				"parentfield": "user_groups",
				"idx": 1,
				"user_group": row.user_group,
			}
		).db_insert()
