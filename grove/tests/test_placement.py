# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Scoring and the policy registry. Pure — a Candidate is a plain dataclass, so there is no site
and nothing to mock, which is the whole point of keeping `grove/placement/` free of frappe."""

import unittest

from grove.placement.base import (
	Candidate,
	PlacementError,
	Scorer,
	fitting_gpus,
	placement_policy,
	sort_key,
)
from grove.placement.scorers import BestFit, FewestReplicas, SpreadRegions, WarmCache, WorstFit

POLICIES = ("balanced", "pack", "spread")


def box(name, **facts):
	return Candidate(inference_server=name, **facts)


class TestThePolicyRegistry(unittest.TestCase):
	"""Mirrors test_engine's registry tests, because it is the same idiom: one place dispatches,
	a miss raises rather than defaulting."""

	def test_every_policy_resolves_to_scorers(self):
		for policy in POLICIES:
			with self.subTest(policy):
				scorers = placement_policy(policy)
				self.assertTrue(scorers)
				for scorer in scorers:
					self.assertIsInstance(scorer, Scorer)

	def test_an_unknown_policy_raises_rather_than_defaulting(self):
		# Falling back to `balanced` would place replicas somewhere nobody asked for and look
		# like it worked — the failure would surface as a capacity mystery days later.
		with self.assertRaises(PlacementError):
			placement_policy("bin-packing")

	def test_a_blank_policy_is_not_silently_a_policy(self):
		with self.assertRaises(PlacementError):
			placement_policy("")

	def test_the_contract_cannot_be_instantiated(self):
		with self.assertRaises(TypeError):
			Scorer()


class TestEachPreference(unittest.TestCase):
	"""One scorer at a time, which is what the composable shape buys."""

	def test_warm_cache_prefers_a_box_that_has_the_weights(self):
		self.assertLess(
			WarmCache().score(box("a", has_local_weights=True)),
			WarmCache().score(box("b", has_local_weights=False)),
		)

	def test_spread_prefers_the_thinner_region(self):
		self.assertLess(
			SpreadRegions().score(box("a", replicas_in_region=0)),
			SpreadRegions().score(box("b", replicas_in_region=3)),
		)

	def test_best_fit_prefers_the_box_with_least_left_over(self):
		# The point: a 2-card replica should not strand an 8-card box.
		self.assertLess(BestFit().score(box("a", surplus=0)), BestFit().score(box("b", surplus=6)))

	def test_worst_fit_is_best_fits_inverse(self):
		snug, roomy = box("a", surplus=0), box("b", surplus=6)
		self.assertLess(BestFit().score(snug), BestFit().score(roomy))
		self.assertLess(WorstFit().score(roomy), WorstFit().score(snug))

	def test_fewest_replicas_prefers_the_quieter_box(self):
		self.assertLess(
			FewestReplicas().score(box("a", active_replicas=1)),
			FewestReplicas().score(box("b", active_replicas=9)),
		)


class TestAPolicyIsItsOrder(unittest.TestCase):
	"""The tuple IS the tiebreak: a later scorer only speaks where every earlier one ties."""

	def winner(self, policy, candidates):
		scorers = placement_policy(policy)
		return min(candidates, key=lambda c: sort_key(c, scorers)).inference_server

	def test_balanced_takes_the_warm_box_over_the_emptier_one(self):
		warm = box("warm", has_local_weights=True, active_replicas=4, surplus=4)
		empty = box("empty", has_local_weights=False, active_replicas=0, surplus=0)
		self.assertEqual(self.winner("balanced", [empty, warm]), "warm")

	def test_spread_takes_the_thin_region_even_off_a_warm_box(self):
		# The two policies differ on exactly this pair, which is why `spread` exists.
		warm = box("warm", has_local_weights=True, replicas_in_region=3)
		far = box("far", has_local_weights=False, replicas_in_region=0)
		self.assertEqual(self.winner("balanced", [far, warm]), "warm")
		self.assertEqual(self.winner("spread", [far, warm]), "far")

	def test_pack_and_balanced_invert_on_the_same_boxes(self):
		snug = box("snug", surplus=0)
		roomy = box("roomy", surplus=6)
		self.assertEqual(self.winner("balanced", [snug, roomy]), "snug")
		self.assertEqual(self.winner("pack", [snug, roomy]), "roomy")

	def test_a_later_scorer_only_speaks_once_the_earlier_ones_tie(self):
		# Both warm, both equally spread, both the same fit — only FewestReplicas is left.
		quiet = box("quiet", has_local_weights=True, active_replicas=1)
		busy = box("busy", has_local_weights=True, active_replicas=8)
		self.assertEqual(self.winner("balanced", [busy, quiet]), "quiet")

	def test_the_whole_sort_key_is_asserted_not_just_the_winner(self):
		scorers = placement_policy("balanced")
		candidate = box("a", has_local_weights=True, replicas_in_region=2, surplus=3, active_replicas=5)
		self.assertEqual(sort_key(candidate, scorers), (0, 2, 3, 5))


class TestFittingGpus(unittest.TestCase):
	CARDS = [
		{"gpu_index": 2, "gpu_model": "NVIDIA H100 80GB HBM3", "vram_gb": 80},
		{"gpu_index": 0, "gpu_model": "NVIDIA L40S", "vram_gb": 48},
		{"gpu_index": 1, "gpu_model": "NVIDIA H100 80GB HBM3", "vram_gb": 80},
	]

	def test_indices_come_back_sorted(self):
		# find_placement takes the first N, so the order here decides which cards are pinned.
		self.assertEqual(fitting_gpus(self.CARDS), (0, 1, 2))

	def test_the_model_filter_matches_on_substring_not_equality(self):
		# Both sides are unvalidated free text — a Machine GPU says "NVIDIA H100 80GB HBM3" and
		# an operator types "h100". Equality would match nothing and reject the whole fleet.
		self.assertEqual(fitting_gpus(self.CARDS, gpu_model="h100"), (1, 2))
		self.assertEqual(fitting_gpus(self.CARDS, gpu_model="NVIDIA H100 80GB HBM3"), (1, 2))

	def test_a_model_nothing_matches_returns_nothing(self):
		self.assertEqual(fitting_gpus(self.CARDS, gpu_model="mi300"), ())

	def test_min_vram_drops_the_small_card(self):
		self.assertEqual(fitting_gpus(self.CARDS, min_vram_gb=80), (1, 2))

	def test_a_blank_filter_keeps_everything(self):
		# Blank is "any card", not "no card" — the field's own description says so.
		self.assertEqual(fitting_gpus(self.CARDS, gpu_model="", min_vram_gb=0), (0, 1, 2))

	def test_a_card_with_no_model_recorded_is_not_matched_by_a_named_filter(self):
		cards = [{"gpu_index": 0, "gpu_model": None, "vram_gb": 80}]
		self.assertEqual(fitting_gpus(cards, gpu_model="h100"), ())
		self.assertEqual(fitting_gpus(cards), (0,))


if __name__ == "__main__":
	unittest.main()
