import frappe
from frappe.model.document import Document

from grove.pricing import settle


class GroveCredit(Document):
	"""One entry in a user's credit ledger: a top-up, or a negative correction with a note.
	Append-only — a wrong entry is corrected by another, never edited or deleted — so the ledger
	is the record and `Grove User.balance` is only its sum less `spent`."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		grove_user: DF.Link
		note: DF.Data | None
	# end: auto-generated types

	def validate(self):
		if not self.amount:
			frappe.throw("0 is not a top-up.")
		if self.amount < 0 and not self.note:
			frappe.throw("A negative entry needs a note saying why.")
		before = self.get_doc_before_save()
		if before and (before.amount != self.amount or before.grove_user != self.grove_user):
			frappe.throw("A ledger entry is never edited — add a correcting entry instead.")

	def on_update(self):
		settle(self.grove_user)

	def on_trash(self):
		frappe.throw("A ledger entry is never deleted — add a correcting entry instead.")
