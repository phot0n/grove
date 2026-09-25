from frappe.model.document import Document


class UsageCounterRow(Document):
	"""One (model, counter) amount on a day's Usage Record (child) — the quantity a rate row prices."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Int
		counter: DF.Literal['input_tokens', 'cached_tokens', 'cache_write_tokens', 'cache_write_1h_tokens', 'completion_tokens', 'audio_seconds', 'request_count']
		model: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types

	pass
