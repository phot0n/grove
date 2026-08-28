# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GroveGroupRow(Document):
	"""One Grove User Group a person belongs to. Membership is a list: every group's models
	are unioned, and the user's own Deny still beats all of them."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		user_group: DF.Link
	# end: auto-generated types

	pass
