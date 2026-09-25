# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ModelGroupRow(Document):
	"""One Model Group a person belongs to. Membership is a list: every group's models
	are unioned, and the user's own Deny still beats all of them."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		model_group: DF.Link
	# end: auto-generated types

	pass
