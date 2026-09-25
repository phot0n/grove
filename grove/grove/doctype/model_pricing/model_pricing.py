import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from grove.pricing import validate_price_rows
from grove.utils import utc_today


class ModelPricing(Document):
	"""The SELL price of one model: a status, the UTC day it takes over, and one rate per counter.
	One way only — enabling a new pricing disables the last, and history prices each day by whichever
	was enabled then. A Scheduled one waits for its day, editable until it fires."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled_on: DF.Date | None
		model: DF.Link
		rates: DF.Table["ModelPricingRate"]
		status: DF.Literal['Disabled', 'Scheduled', 'Enabled']
	# end: auto-generated types

	def validate(self):
		validate_price_rows(self.rates, key=lambda row: row.counter)
		before = self.get_doc_before_save()
		was_scheduled = bool(before and before.status == "Scheduled")
		if before and before.enabled_on and not was_scheduled:
			self.validate_frozen(before)
			return
		if self.status == "Scheduled":
			self.validate_scheduled()
		elif self.status == "Enabled":
			self.enable(was_scheduled)
		else:
			# Disabled with no window yet: a cancelled schedule, or a typed date that would otherwise price.
			self.enabled_on = None

	def validate_frozen(self, before):
		"""Once enabled, a pricing is history: the days it priced are billed. A wrong price is a
		new pricing plus a credit row, never an edit here."""
		if before.status == "Enabled" and self.status == "Disabled":
			frappe.throw("Enable a successor instead — disabled by hand, the model goes unpriced.")
		if before.status == "Disabled" and self.status != "Disabled":
			frappe.throw("This pricing already had its window. Duplicate it to price again.")
		rates = [(row.counter, row.rate) for row in self.rates]
		if self.model != before.model or rates != [(row.counter, row.rate) for row in before.rates]:
			frappe.throw("An enabled pricing cannot change. Enable a new one and credit the days before.")

	def validate_scheduled(self):
		if not self.enabled_on or getdate(self.enabled_on) <= utc_today():
			frappe.throw("A scheduled pricing needs a day ahead of today — to price from today, enable it.")
		other = frappe.db.exists(
			"Model Pricing", {"model": self.model, "status": "Scheduled", "name": ("!=", self.name)}
		)
		if other:
			frappe.throw(f"{other} is already scheduled for {self.model} — one at a time.")

	def enable(self, was_scheduled):
		"""Takes over today, or on its scheduled day once that day has come. A typed date on an
		unscheduled doc would backdate, so it is refused."""
		if was_scheduled:
			self.enabled_on = min(getdate(self.enabled_on), utc_today())
		elif self.enabled_on:
			frappe.throw("Enabled On is stamped on enable. To take over on a later day, set Scheduled.")
		else:
			self.enabled_on = utc_today()
		self.flags.enabling = True

	def on_update(self):
		"""Enabling takes over from 00:00 UTC of its day: the predecessor steps down in the same save
		and every prepaid balance is re-priced for the day."""
		if not self.flags.enabling:
			return
		frappe.db.set_value(
			"Model Pricing", {"model": self.model, "status": "Enabled", "name": ("!=", self.name)}, "status", "Disabled"
		)
		frappe.enqueue("grove.pricing.verify_balances", queue="long", enqueue_after_commit=True)


def enable_due():
	"""Every minute: a Scheduled pricing whose day has come goes Enabled and retires its
	predecessor. One failure skips nobody."""
	due = frappe.get_all(
		"Model Pricing", filters={"status": "Scheduled", "enabled_on": ("<=", utc_today())}, pluck="name"
	)
	for name in due:
		try:
			doc = frappe.get_doc("Model Pricing", name)
			doc.status = "Enabled"
			doc.save()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Scheduled pricing {name} failed to enable"[:140])
