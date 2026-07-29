# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.grove.doctype.grove_api_key.grove_api_key import mark_keys_dirty


class GroveUserGroup(Document):
	"""A named set of models. Membership lives on Grove User, which links here."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		description: DF.SmallText | None
		models: DF.Table[GroveModelRow]
		priority: DF.Int
	# end: auto-generated types

	def on_update(self):
		# The model list or the priority moved → every member's keys carry a stale
		# projection. Joining or leaving is an edit on Grove User, whose own on_update
		# dirties that one user.
		mark_keys_dirty(self.member_users)

	def on_trash(self):
		mark_keys_dirty(self.member_users)

	@property
	def member_users(self):
		return frappe.get_all("Grove User", {"user_group": self.name}, pluck="name")
