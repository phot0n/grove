# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class UsageGatewayRow(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cached_tokens: DF.Int
		completion_tokens: DF.Int
		gateway_server: DF.Link | None
		last_pulled: DF.Datetime | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		prompt_tokens: DF.Int
		request_count: DF.Int
		total_tokens: DF.Int
	# end: auto-generated types

	pass
