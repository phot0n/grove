# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Usage aggregation. Pure — the rows are passed in, so no site needed."""

import unittest

from grove.api import _totals_by_model as totals_by_model
from grove.grove.doctype.usage_record.usage_record import billable

FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "request_count")


def row(model, prompt=0, completion=0, cached=0, requests=0):
	return {
		"model": model,
		"prompt_tokens": prompt,
		"completion_tokens": completion,
		"cached_tokens": cached,
		"request_count": requests,
	}


class TestTotalsByModel(unittest.TestCase):
	def test_no_rows_is_no_summary(self):
		self.assertEqual(totals_by_model([], FIELDS), [])

	def test_rows_for_one_model_are_summed_not_overwritten(self):
		# A user's keys each hold their own monthly record, so the same model arrives twice.
		summary = totals_by_model(
			[row("qwen3-35b", prompt=10, completion=20, requests=1),
			 row("qwen3-35b", prompt=5, completion=10, requests=2)],
			FIELDS,
		)
		self.assertEqual(len(summary), 1)
		self.assertEqual(summary[0]["prompt_tokens"], 15)
		self.assertEqual(summary[0]["completion_tokens"], 30)
		self.assertEqual(summary[0]["request_count"], 3)

	def test_biggest_consumer_comes_first(self):
		summary = totals_by_model(
			[row("small", prompt=10), row("big", prompt=900), row("mid", prompt=100)], FIELDS
		)
		self.assertEqual([t["model"] for t in summary], ["big", "mid", "small"])

	def test_the_model_is_named_in_each_entry(self):
		summary = totals_by_model([row("qwen3-35b", prompt=1)], FIELDS)
		self.assertEqual(summary[0]["model"], "qwen3-35b")

	def test_missing_metrics_count_as_zero(self):
		# get_all can hand back None for a column never written.
		summary = totals_by_model([{"model": "qwen3-35b", "prompt_tokens": None}], FIELDS)
		self.assertEqual(summary[0]["prompt_tokens"], 0)


class TestBillable(unittest.TestCase):
	"""The one definition the budget gate and the usage report both use."""

	def test_cache_hits_do_not_count_against_a_budget(self):
		self.assertEqual(billable(100, 25, 40), 85)

	def test_a_cache_overcount_never_eats_completion(self):
		# One malformed record must not credit a user back under their limit.
		self.assertEqual(billable(0, 0, 90), 0)
		self.assertEqual(billable(10, 5, 90), 5)

	def test_a_column_never_written_reads_as_zero(self):
		self.assertEqual(billable(None, None, None), 0)
		self.assertEqual(billable(50, None, None), 50)


if __name__ == "__main__":
	unittest.main()
