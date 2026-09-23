# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class UsageRecord(Document):
	"""One (key, UTC day). The counter rows are the record — what the rate tables price and the
	reports read; the gateway rows say which box served the key. Written by the pull and by
	nothing else."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.usage_counter_row.usage_counter_row import UsageCounterRow
		from grove.grove.doctype.usage_gateway_row.usage_gateway_row import UsageGatewayRow

		api_key: DF.Link
		counter_usage: DF.Table[UsageCounterRow]
		day: DF.Date
		gateway_usage: DF.Table[UsageGatewayRow]
		user: DF.Link | None
	# end: auto-generated types
