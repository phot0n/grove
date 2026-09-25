from frappe.model.document import Document


class ModelPriceRow(Document):
	"""One dated COST rate on a provider's rate card (child): what the vendor charges us for
	`provider_model_id`, per counter, from `effective_from`."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		counter: DF.Literal['input_tokens', 'cached_tokens', 'cache_write_tokens', 'cache_write_1h_tokens', 'completion_tokens', 'audio_seconds', 'request_count']
		effective_from: DF.Date
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		provider_model_id: DF.Data
		rate: DF.Currency
	# end: auto-generated types

	pass
