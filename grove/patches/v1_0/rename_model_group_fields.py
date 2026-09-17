from frappe.model.utils.rename_field import rename_field


def execute():
	# A table field has no column of its own, which validation would take for a field that never existed.
	rename_field("Grove User", "user_groups", "model_groups", validate=False)
	rename_field("Model Group Row", "user_group", "model_group")
