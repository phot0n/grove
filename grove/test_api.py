# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Usage aggregation. Pure — the rows are passed in, so no site needed."""

import unittest

from grove.api import totals_by_model

FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "request_count")


def row(model, prompt=0, completion=0, cached=0, total=0, requests=0):
	return {
		"model": model,
		"prompt_tokens": prompt,
		"completion_tokens": completion,
		"cached_tokens": cached,
		"total_tokens": total,
		"request_count": requests,
	}


class TestTotalsByModel(unittest.TestCase):
	def test_no_rows_is_no_summary(self):
		self.assertEqual(totals_by_model([], FIELDS), [])

	def test_rows_for_one_model_are_summed_not_overwritten(self):
		# A user's keys each hold their own monthly record, so the same model arrives twice.
		summary = totals_by_model(
			[row("qwen3-35b", prompt=10, total=30, requests=1),
			 row("qwen3-35b", prompt=5, total=15, requests=2)],
			FIELDS,
		)
		self.assertEqual(len(summary), 1)
		self.assertEqual(summary[0]["prompt_tokens"], 15)
		self.assertEqual(summary[0]["total_tokens"], 45)
		self.assertEqual(summary[0]["request_count"], 3)

	def test_billable_excludes_cached_tokens(self):
		summary = totals_by_model([row("qwen3-35b", total=100, cached=40)], FIELDS)
		self.assertEqual(summary[0]["billable_tokens"], 60)

	def test_biggest_consumer_comes_first(self):
		summary = totals_by_model(
			[row("small", total=10), row("big", total=900), row("mid", total=100)], FIELDS
		)
		self.assertEqual([t["model"] for t in summary], ["big", "mid", "small"])

	def test_the_model_is_named_in_each_entry(self):
		summary = totals_by_model([row("qwen3-35b", total=1)], FIELDS)
		self.assertEqual(summary[0]["model"], "qwen3-35b")

	def test_missing_metrics_count_as_zero(self):
		# get_all can hand back None for a column never written.
		summary = totals_by_model([{"model": "qwen3-35b", "total_tokens": None}], FIELDS)
		self.assertEqual(summary[0]["total_tokens"], 0)
		self.assertEqual(summary[0]["billable_tokens"], 0)


if __name__ == "__main__":
	unittest.main()
