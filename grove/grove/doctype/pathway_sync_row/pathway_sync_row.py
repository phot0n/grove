# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PathwaySyncRow(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		detail: DF.Data | None
		duration_ms: DF.Int
		error: DF.SmallText | None
		had_data: DF.Check
		http_status: DF.Int
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payload: DF.LongText | None
		reachable: DF.Check
		server: DF.DynamicLink | None
		server_type: DF.Link | None
		success: DF.Check
	# end: auto-generated types

	pass
