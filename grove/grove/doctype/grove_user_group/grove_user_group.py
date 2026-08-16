# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GroveUserGroup(Document):
	"""A named set of models. Membership lives on Grove User, which links here.

	The gateway holds this as its own Redis record (group:<name>) and each member's user record
	points at it, so an edit here is one push however many members the group has. No sync hook:
	any edit, or the delete itself, moves the snapshot hash and the next tick pushes it — a
	deleted group is pruned because it is simply no longer named."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		description: DF.SmallText | None
		models: DF.Table[GroveModelRow]
		public_catalog: DF.Check
	# end: auto-generated types
