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
		# The standard bin-packing names, meaning the standard things: best fit takes the tightest
		# box and so CONSOLIDATES; worst fit takes the emptiest and so DISTRIBUTES. Getting these
		# the wrong way round is how `pack` once spread.
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

	def test_every_policy_ends_in_a_total_order(self):
		# Two boxes alike in everything a policy reads would sort arbitrarily, and placement
		# would wander between them run to run. Each policy's last scorer has to break that.
		a = box("a", surplus=2, active_replicas=1)
		b = box("b", surplus=2, active_replicas=3)
		for policy in POLICIES:
			with self.subTest(policy):
				scorers = placement_policy(policy)
				self.assertNotEqual(sort_key(a, scorers), sort_key(b, scorers))

	def test_pack_consolidates_and_spread_distributes(self):
		# The property the policy NAMES have to hold, and the one that was wrong: packing takes
		# the tightest box so replicas gather, spreading takes the emptiest so they scatter.
		snug = box("snug", surplus=0)
		roomy = box("roomy", surplus=6)
		self.assertEqual(self.winner("pack", [snug, roomy]), "snug")
		self.assertEqual(self.winner("spread", [snug, roomy]), "roomy")

	def test_pack_ignores_region_and_balanced_does_not(self):
		# The only thing separating them: balanced weighs region before fit, pack not at all.
		crowded_but_snug = box("snug", replicas_in_region=5, surplus=0)
		empty_region = box("far", replicas_in_region=0, surplus=6)
		self.assertEqual(self.winner("balanced", [crowded_but_snug, empty_region]), "far")
		self.assertEqual(self.winner("pack", [crowded_but_snug, empty_region]), "snug")

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
	# Deliberately named so that docname order and index order DISAGREE: `cards_on` sorts by CUDA
	# index and "take the first N" means that order, so sorting the names would pin other cards.
	CARDS = [
		{"name": "aaa", "gpu_index": 2, "gpu_type": "h100", "vram_gb": 80},
		{"name": "zzz", "gpu_index": 0, "gpu_type": "l40s", "vram_gb": 48},
		{"name": "mmm", "gpu_index": 1, "gpu_type": "h100", "vram_gb": 80},
	]

	def test_cards_come_back_in_the_order_they_were_given(self):
		# find_placement takes the first N, so this order decides which cards are pinned. The
		# caller hands them over index-ordered; sorting docnames here would order them by a hash.
		self.assertEqual(fitting_gpus(self.CARDS), ("aaa", "zzz", "mmm"))

	def test_a_card_is_named_not_numbered(self):
		# The point of the whole change: an index has to be resolved against the box a second
		# time, and a scan landing in between resolves it onto different silicon.
		self.assertEqual(fitting_gpus([self.CARDS[0]]), ("aaa",))

	def test_the_type_filter_is_exact(self):
		# It used to be a substring test, because both sides were unvalidated free text —
		# nvidia-smi said "Tesla T4" where AWS said "T4". Both resolve to one GPU Type now, so
		# equality is finally safe and there is nothing left to guess at.
		self.assertEqual(fitting_gpus(self.CARDS, gpu_type="h100"), ("aaa", "mmm"))
		self.assertEqual(fitting_gpus(self.CARDS, gpu_type="l40s"), ("zzz",))

	def test_a_type_nothing_matches_returns_nothing(self):
		self.assertEqual(fitting_gpus(self.CARDS, gpu_type="mi300"), ())

	def test_min_vram_drops_the_small_card(self):
		self.assertEqual(fitting_gpus(self.CARDS, min_vram_gb=80), ("aaa", "mmm"))

	def test_a_blank_filter_keeps_everything(self):
		# Blank is "any card", not "no card" — the field's own description says so.
		self.assertEqual(fitting_gpus(self.CARDS, gpu_type="", min_vram_gb=0), ("aaa", "zzz", "mmm"))

	def test_a_card_with_no_type_resolved_is_not_matched_by_a_named_filter(self):
		# A card whose type could not be resolved must not satisfy a filter that names one —
		# placing on it would be guessing at hardware nobody identified.
		cards = [{"name": "aaa", "gpu_index": 0, "gpu_type": None, "vram_gb": 80}]
		self.assertEqual(fitting_gpus(cards, gpu_type="h100"), ())
		self.assertEqual(fitting_gpus(cards), ("aaa",))


if __name__ == "__main__":
	unittest.main()
