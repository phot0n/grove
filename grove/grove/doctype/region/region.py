# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class Region(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cloud_provider: DF.Literal["", "runpod", "aws"]
		geography: DF.Link
		label: DF.Data | None
		remote_write_url: DF.Data | None
	# end: auto-generated types

	def validate(self):
		if self.is_new() or not self.has_value_changed("geography"):
			return
		for doctype in ("Network", "Machine"):
			if frappe.db.exists(doctype, {"region": self.name}):
				frappe.throw(
					f"A {doctype} is in {self.name}, and every box under it carries this geography — "
					"a region cannot move between geographies once it holds boxes."
				)

	def gateways(self, exclude=None):
		"""Every gateway still in this region, by name."""
		filters = {"region": self.name, "status": ("!=", "Terminated")}
		if exclude:
			filters["name"] = ("!=", exclude)
		return frappe.get_all("Gateway Server", filters=filters, pluck="name")
