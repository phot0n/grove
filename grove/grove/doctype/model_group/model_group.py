# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ModelGroup(Document):
	"""A named set of models. Membership lives on Grove User, and a user reaches the union of every
	group it lists.

	The gateway holds this as its own Redis record and each member's user record names it, so an
	edit here is ONE push however many members the group has."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		description: DF.Data | None
		models: DF.Table[GroveModelRow]
	# end: auto-generated types

	def validate(self):
		# The name travels inside a comma-joined membership list, so a comma would split it into
		# two groups that resolve to nothing.
		if "," in self.name:
			frappe.throw("A group name cannot contain a comma")
