# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class UsageGatewayRow(Document):
	"""Requests one Redis (a Gateway Store, or a box on its own) served for this key that day,
	and when it was last drained. Gateways on a store share their counters, so a box is never
	the unit here."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		last_pulled: DF.Datetime | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		redis: DF.Data | None
		request_count: DF.Int
	# end: auto-generated types

	pass
