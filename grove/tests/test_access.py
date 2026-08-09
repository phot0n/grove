# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The one piece of access policy Grove still computes: the priority sign flip. Precedence between
a group's grant and a user's allow/deny is resolved by the gateway now (gateway_service/eval.go),
and asserted there."""

import unittest

from grove.access import vllm_priority


class TestVllmPriority(unittest.TestCase):
	def test_a_more_important_group_sorts_ahead_of_the_baseline(self):
		# vLLM serves the lowest first, so "more important" has to come out negative.
		self.assertLess(vllm_priority(10), vllm_priority(0))

	def test_no_group_is_the_baseline(self):
		self.assertEqual(vllm_priority(0), 0)
		self.assertEqual(vllm_priority(None), 0)

	def test_a_group_below_the_baseline_falls_behind_it(self):
		self.assertGreater(vllm_priority(-5), vllm_priority(0))


if __name__ == "__main__":
	unittest.main()
