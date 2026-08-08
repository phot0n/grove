# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.grove.doctype.grove_api_key.grove_api_key import mark_keys_dirty


class GroveUserGroup(Document):
	"""A named set of models. Membership lives on Grove User, which links here.

	The gateway holds this as its own Redis record (group:<name>) and each key points at it, so
	an edit here is one push however many members the group has."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		description: DF.SmallText | None
		dirty: DF.Check
		models: DF.Table[GroveModelRow]
		priority: DF.Int
		public_catalog: DF.Check
	# end: auto-generated types

	def on_update(self):
		# The model list, the priority or the catalogue flag moved → this group's own record is
		# stale. The members' keys are not: they carry the group's NAME, which has not changed.
		# Joining or leaving is an edit on Grove User, whose own on_update dirties it.
		if not self.dirty:
			frappe.db.set_value(self.doctype, self.name, "dirty", 1, update_modified=False)

	def on_trash(self):
		# The keys have to stop naming a group that no longer exists — otherwise they keep
		# resolving against whatever its Redis record still says. Frappe blocks deleting a
		# linked group, so this normally has nothing to do.
		mark_keys_dirty(self.member_users)
		if self.public_catalog:
			# The anonymous catalogue is a single pushed list, recomputed from the groups that
			# still exist. Nothing else dirties on a delete, so without this the models of a
			# deleted group keep being advertised until some other group happens to change.
			frappe.enqueue(
				"grove.gateway_sync.full_sync", queue="short", trigger="Group Deleted"
			)

	@property
	def member_users(self):
		return frappe.get_all("Grove User", {"user_group": self.name}, pluck="name")
