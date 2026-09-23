# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Usage aggregation. Pure — the rows are passed in, so no site needed."""

import unittest

from grove.api import _token_rows as token_rows
from grove.api import _totals_by_model as totals_by_model
from grove.api import _totals_by_user as totals_by_user

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


class TestTokenRows(unittest.TestCase):
	"""Counter rows folded back into the three token columns the report has always returned."""

	def test_every_prompt_side_counter_is_a_prompt_token(self):
		[row] = token_rows([
			{"model": "m", "counter": "input_tokens", "amount": 60},
			{"model": "m", "counter": "cached_tokens", "amount": 30},
			{"model": "m", "counter": "cache_write_tokens", "amount": 10},
			{"model": "m", "counter": "completion_tokens", "amount": 5},
			{"model": "m", "counter": "request_count", "amount": 2},
		])
		self.assertEqual(row, {"model": "m", "prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 30})

	def test_rows_from_several_records_fold_per_model(self):
		rows = token_rows([
			{"model": "m", "counter": "completion_tokens", "amount": 1},
			{"model": "m", "counter": "completion_tokens", "amount": 2},
			{"model": "n", "counter": "completion_tokens", "amount": None},
		])
		self.assertEqual({r["model"]: r["completion_tokens"] for r in rows}, {"m": 3, "n": 0})


class TestTotalsByUser(unittest.TestCase):
	"""Per-user token totals come from the counter rows, through the records that name the user."""

	def test_rows_fold_onto_the_records_user_and_a_rowless_record_is_zeros(self):
		usage = totals_by_user(
			[
				{"parent": "R1", "model": "m", "counter": "input_tokens", "amount": 6},
				{"parent": "R1", "model": "n", "counter": "completion_tokens", "amount": 1},
				{"parent": "R2", "model": "m", "counter": "cached_tokens", "amount": 4},
			],
			{"R1": "a@x", "R2": "a@x", "R3": "b@x"},
		)
		self.assertEqual(usage["a@x"], {"prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 4})
		self.assertEqual(usage["b@x"], {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})


if __name__ == "__main__":
	unittest.main()
