# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Access precedence. Pure — the grants are passed in, so no site needed."""

import unittest

from grove.access import resolve, vllm_priority


class TestResolve(unittest.TestCase):
	def test_no_grant_is_no_models(self):
		self.assertEqual(resolve([], [], []), set())

	def test_group_grants(self):
		self.assertEqual(resolve(["qwen3-35b"], [], []), {"qwen3-35b"})

	def test_user_allow_grants_without_a_group(self):
		self.assertEqual(resolve([], ["qwen3-35b"], []), {"qwen3-35b"})

	def test_grants_from_several_groups_add_up(self):
		self.assertEqual(resolve(["a", "b"], ["c"], []), {"a", "b", "c"})

	def test_deny_beats_a_group_grant(self):
		self.assertEqual(resolve(["a", "b"], [], ["b"]), {"a"})

	def test_deny_beats_the_users_own_allow(self):
		self.assertEqual(resolve([], ["a"], ["a"]), set())

	def test_deny_of_an_ungranted_model_is_harmless(self):
		self.assertEqual(resolve(["a"], [], ["zzz"]), {"a"})


class TestVllmPriority(unittest.TestCase):
	def test_a_more_important_group_sorts_ahead_of_the_baseline(self):
		# vLLM serves the lowest first, so "more important" has to come out negative.
		self.assertLess(vllm_priority(10), vllm_priority(0))

	def test_no_group_is_the_baseline(self):
		self.assertEqual(vllm_priority(0), 0)
		self.assertEqual(vllm_priority(None), 0)

	def test_a_group_below_the_baseline_falls_behind_it(self):
		self.assertGreater(vllm_priority(-5), vllm_priority(0))
