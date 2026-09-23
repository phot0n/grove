# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Prices per counter. Pure — the rate rows are handed to the book, no site."""

import unittest
from datetime import date, datetime
from decimal import Decimal

import frappe

from grove import pricing
from grove.pricing import PriceBook, effective_rate

D = Decimal
JAN = date(2026, 1, 1)
APR15 = date(2026, 4, 15)


def row(day, counter, rate, creation="2026-01-01 00:00:00"):
	return (day, counter, D(rate), datetime.fromisoformat(creation))


def book(models=(), sell=None):
	b = PriceBook()
	b.models = set(models)
	b.sell = sell or {}
	return b


class TestEffectiveRate(unittest.TestCase):
	ROWS = [row(JAN, "completion_tokens", "1"), row(APR15, "completion_tokens", "3")]

	def test_the_newest_row_on_or_before_the_day_wins(self):
		self.assertEqual(effective_rate(self.ROWS, "completion_tokens", date(2026, 4, 14)), D("1"))
		self.assertEqual(effective_rate(self.ROWS, "completion_tokens", APR15), D("3"))

	def test_before_every_row_is_unpriced(self):
		self.assertIsNone(effective_rate(self.ROWS, "completion_tokens", date(2025, 12, 31)))

	def test_another_counter_does_not_price_this_one(self):
		self.assertIsNone(effective_rate(self.ROWS, "cached_tokens", APR15))

	def test_the_same_day_twice_goes_to_the_newer_doc(self):
		rows = [row(JAN, "completion_tokens", "1", "2026-01-01 09:00:00"), row(JAN, "completion_tokens", "2", "2026-01-01 10:00:00")]
		self.assertEqual(effective_rate(rows, "completion_tokens", JAN), D("2"))


class TestUsageCost(unittest.TestCase):
	RATES = {"input_tokens": "3", "cached_tokens": "0.3", "cache_write_tokens": "3.75",
	         "cache_write_1h_tokens": "6", "completion_tokens": "15"}

	def priced(self):
		return book(models=["anthropic/claude"],
		            sell={"anthropic/claude": [row(JAN, c, r) for c, r in self.RATES.items()]})

	def test_a_million_prompt_tokens_split_across_the_cache_counters(self):
		counts = {"input_tokens": 400_000, "cached_tokens": 400_000, "cache_write_tokens": 100_000,
		          "cache_write_1h_tokens": 100_000, "completion_tokens": 500_000}
		self.assertEqual(self.priced().usage_cost(counts, "anthropic/claude", APR15), D("9.795"))

	def test_audio_is_priced_per_minute(self):
		b = book(models=["frappe/whisper"], sell={"frappe/whisper": [row(JAN, "audio_seconds", "0.006")]})
		self.assertEqual(b.usage_cost({"audio_seconds": 90}, "frappe/whisper", APR15), D("0.009"))

	def test_a_counter_with_no_rate_bills_zero(self):
		b = self.priced()
		self.assertEqual(b.usage_cost({"audio_seconds": 60, "completion_tokens": 1_000_000}, "anthropic/claude", APR15), D("15"))
		self.assertEqual(b.usage_cost({"audio_seconds": 60}, "anthropic/claude", APR15), D("0"))

	def test_rates_for_the_push_are_whole_nano_usd(self):
		b = book(models=["m"], sell={"m": [row(JAN, "completion_tokens", "0.3"), row(JAN, "request_count", "0")]})
		self.assertEqual(b.rates_for("m", APR15), {"completion_tokens": 300_000_000, "request_count": 0})

	def test_decimal_arithmetic_lands_exactly_on_zero(self):
		b = book(models=["m"], sell={"m": [row(JAN, "completion_tokens", "0.075")]})
		self.assertEqual(D("225") - b.usage_cost({"completion_tokens": 3_000_000_000}, "m", APR15), D("0"))


class TestMoneyUnits(unittest.TestCase):
	def test_nano_reads_a_float_as_its_decimal_text(self):
		self.assertEqual(pricing.nano(0.3), 300_000_000)
		self.assertEqual(pricing.nano(D("1.000000001")), 1_000_000_001)

	def test_the_micro_floor_drops_only_the_sub_micro_part(self):
		self.assertEqual(pricing.micro_floor(1_234_567), 1_234_000)
		self.assertEqual(pricing.micro_floor(-1_234_567), -1_235_000)

	def test_tolerance_is_a_micro_usd_per_request(self):
		self.assertEqual(pricing.tolerance(3), D("0.000003"))


class TestValidatePriceRows(unittest.TestCase):
	def rows(self, *specs):
		return [frappe._dict(idx=i + 1, counter=c, rate=r) for i, (c, r) in enumerate(specs)]

	def test_a_duplicate_counter_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pricing.validate_price_rows(self.rows(("completion_tokens", 1), ("completion_tokens", 2)), key=lambda r: r.counter)

	def test_a_negative_rate_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pricing.validate_price_rows(self.rows(("completion_tokens", -1)), key=lambda r: r.counter)

	def test_a_blank_rate_is_refused_but_zero_is_a_price(self):
		with self.assertRaises(frappe.ValidationError):
			pricing.validate_price_rows(self.rows(("completion_tokens", None)), key=lambda r: r.counter)
		pricing.validate_price_rows(self.rows(("completion_tokens", 0)), key=lambda r: r.counter)


if __name__ == "__main__":
	unittest.main()
