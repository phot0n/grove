# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""A pricing is enabled once and never edited: the days it priced are billed. A Scheduled one
waits for its day, editable until it fires."""

import unittest.mock
from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model_pricing import model_pricing
from grove.pricing import PriceBook
from grove.utils import utc_today


class TestModelPricing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "pricing-7b", "modality": "text", "hf_repo": "org/pricing-7b"}
		).insert(ignore_permissions=True).name

	def pricing(self, status="Enabled", enabled_on=None, **rates):
		return frappe.get_doc({
			"doctype": "Model Pricing", "model": self.model, "status": status, "enabled_on": enabled_on,
			"rates": [{"counter": counter, "rate": rate} for counter, rate in rates.items()],
		}).insert(ignore_permissions=True)

	def scheduled(self, days=2, **rates):
		"""One Scheduled doc per model, so each test's is cancelled behind it."""
		doc = self.pricing("Scheduled", enabled_on=utc_today() + timedelta(days=days), **rates)
		self.addCleanup(frappe.db.set_value, "Model Pricing", doc.name, "status", "Disabled")
		return doc

	def test_enabling_stamps_today_and_disables_the_predecessor(self):
		first = self.pricing(completion_tokens=1)
		second = self.pricing(completion_tokens=2)
		first.reload()
		self.assertEqual((first.status, second.status), ("Disabled", "Enabled"))
		self.assertEqual(second.enabled_on, utc_today())
		self.assertEqual(first.enabled_on, utc_today())

	def test_the_predecessor_still_prices_the_days_before(self):
		first = self.pricing(completion_tokens=1)
		second = self.pricing(completion_tokens=2)
		frappe.db.set_value("Model Pricing", first.name, "enabled_on", utc_today() - timedelta(days=10))
		book = PriceBook.load()
		self.assertEqual(book.rate_for(self.model, "completion_tokens", utc_today() - timedelta(days=1)), 1)
		self.assertEqual(book.rate_for(self.model, "completion_tokens", utc_today()), 2)
		self.assertEqual(second.enabled_on, utc_today())

	def test_two_enabled_the_same_day_the_newer_doc_wins(self):
		self.pricing(completion_tokens=1)
		self.pricing(completion_tokens=2)
		self.assertEqual(PriceBook.load().rate_for(self.model, "completion_tokens", utc_today()), 2)

	def test_re_enabling_disabling_by_hand_and_editing_are_refused(self):
		doc = self.pricing(completion_tokens=1)
		doc.status = "Disabled"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		frappe.db.set_value("Model Pricing", doc.name, "status", "Disabled")
		doc.reload()
		doc.status = "Enabled"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc.reload()
		doc.rates[0].rate = 9
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_enabled_on_is_stamped_never_typed(self):
		with self.assertRaises(frappe.ValidationError):
			self.pricing("Enabled", enabled_on=utc_today() - timedelta(days=3), completion_tokens=1)

	def test_a_counter_missing_from_the_enabled_doc_is_unpriced_whatever_the_cost_card_says(self):
		provider = frappe.get_doc("Model", self.model).provider
		provider_doc = frappe.get_doc("Model Provider", provider)
		provider_doc.append("rate_card", {"provider_model_id": "pricing-7b", "counter": "input_tokens", "rate": 0.5})
		provider_doc.save(ignore_permissions=True)
		self.pricing(completion_tokens=1)
		book = PriceBook.load()
		self.assertIsNone(book.rate_for(self.model, "input_tokens", utc_today()))
		self.assertEqual(book.rate_for(self.model, "completion_tokens", utc_today()), 1)

	def test_scheduling_needs_a_day_ahead(self):
		for day in (None, utc_today(), utc_today() - timedelta(days=1)):
			with self.assertRaises(frappe.ValidationError):
				self.pricing("Scheduled", enabled_on=day, completion_tokens=1)

	def test_one_scheduled_per_model(self):
		self.scheduled(completion_tokens=1)
		with self.assertRaises(frappe.ValidationError):
			self.pricing("Scheduled", enabled_on=utc_today() + timedelta(days=3), completion_tokens=2)

	def test_a_scheduled_pricing_stays_editable_and_prices_nothing_yet(self):
		doc = self.scheduled(completion_tokens=1)
		doc.rates[0].rate = 5
		doc.enabled_on = utc_today() + timedelta(days=9)
		doc.save()
		self.assertEqual((doc.status, doc.rates[0].rate), ("Scheduled", 5))
		self.assertNotIn(doc.enabled_on, [day for day, *_ in PriceBook.load().sell.get(self.model, [])])

	def test_cancelling_a_schedule_clears_its_day(self):
		doc = self.scheduled(completion_tokens=1)
		doc.status = "Disabled"
		doc.save()
		self.assertIsNone(doc.enabled_on)

	def test_a_due_schedule_fires_and_retires_the_predecessor(self):
		first = self.pricing(completion_tokens=1)
		doc = self.scheduled(completion_tokens=2)
		frappe.db.set_value("Model Pricing", doc.name, "enabled_on", utc_today())
		with (
			unittest.mock.patch.object(frappe, "enqueue") as enqueue,
			unittest.mock.patch.object(frappe.db, "commit"),
		):
			model_pricing.enable_due()
		doc.reload()
		first.reload()
		self.assertEqual((doc.status, doc.enabled_on, first.status), ("Enabled", utc_today(), "Disabled"))
		enqueue.assert_called_once()

	def test_a_schedule_for_tomorrow_waits(self):
		doc = self.scheduled(days=1, completion_tokens=1)
		with unittest.mock.patch.object(frappe.db, "commit"):
			model_pricing.enable_due()
		doc.reload()
		self.assertEqual(doc.status, "Scheduled")
