# Copyright (c) 2026, Grove and contributors
# See license.txt
"""How a server names itself. Pure — the region lookup and the counter are both injected.

The name is not a label, it is infrastructure: a Gateway Server's and an Ingress Server's name IS
its record under the fleet zone, and every server's name is part of the request ids it stamps. So
what is pinned here is that it stays one DNS label, that each region counts on its own, and that
the counter is asked for a number rather than derived from what already exists.
"""

import unittest
from unittest.mock import patch

import frappe

from grove.naming import next_replica_name, next_server_name
from grove.utils import is_id_safe, is_label_under, slugify

ZONE = "grove.example.com"


class FakeSeries:
	"""Stands in for tabSeries: one climbing counter per key, which is the whole contract."""

	def __init__(self):
		self.current = {}

	def __call__(self, key, digits):
		self.current[key] = self.current.get(key, 0) + 1
		return ("%0" + str(digits) + "d") % self.current[key]


def name(series, region="ap-south-1", prefix="gw"):
	with patch.object(frappe, "db", frappe._dict(get_value=lambda *a, **k: region)):
		return next_server_name(prefix, "MACHINE-1", counter=series)


class TestServerNaming(unittest.TestCase):
	def setUp(self):
		self.series = FakeSeries()

	def test_the_first_server_in_a_region_is_one(self):
		self.assertEqual(name(self.series), "gw1-ap-south-1")

	def test_the_next_one_counts_up(self):
		self.assertEqual(
			[name(self.series) for _ in range(3)],
			["gw1-ap-south-1", "gw2-ap-south-1", "gw3-ap-south-1"],
		)

	def test_each_region_counts_on_its_own(self):
		# The point of keying the series on the region. A single counter would give
		# gw1-ap-south-1 then gw2-us-east-1, and the number would stop meaning anything local.
		self.assertEqual(name(self.series, region="ap-south-1"), "gw1-ap-south-1")
		self.assertEqual(name(self.series, region="us-east-1"), "gw1-us-east-1")
		self.assertEqual(name(self.series, region="ap-south-1"), "gw2-ap-south-1")
		self.assertEqual(name(self.series, region="us-east-1"), "gw2-us-east-1")

	def test_each_kind_of_server_counts_on_its_own(self):
		self.assertEqual(name(self.series, prefix="gw"), "gw1-ap-south-1")
		self.assertEqual(name(self.series, prefix="ing"), "ing1-ap-south-1")
		self.assertEqual(name(self.series, prefix="inf"), "inf1-ap-south-1")

	def test_a_box_with_no_region_gets_no_suffix(self):
		# A colo machine is in no Network and so has no region — which is what the name should
		# say, rather than inventing one or trailing a bare '-'.
		self.assertEqual(name(self.series, region=""), "gw1")
		self.assertEqual(name(self.series, region=""), "gw2")

	def test_a_regionless_name_counts_apart_from_a_region(self):
		self.assertEqual(name(self.series, region=""), "gw1")
		self.assertEqual(name(self.series), "gw1-ap-south-1")

	def test_the_number_is_asked_for_not_derived_from_what_exists(self):
		# A retired name is never handed to another box: the series climbs whether or not the doc
		# that took the number is still there, where a max-of-existing-plus-one would reuse it.
		self.assertEqual(name(self.series), "gw1-ap-south-1")
		self.series.current["gw-ap-south-1-"] = 7  # as if 2..7 were taken and then deleted
		self.assertEqual(name(self.series), "gw8-ap-south-1")

	def test_the_series_key_is_scoped_and_looks_like_frappes_own(self):
		name(self.series, prefix="ing", region="us-east-1")
		self.assertEqual(list(self.series.current), ["ing-us-east-1-"])

	def test_every_prefix_produces_a_usable_name(self):
		for prefix in ("gw", "ing", "inf"):
			with self.subTest(prefix):
				generated = name(FakeSeries(), prefix=prefix)
				self.assertTrue(generated.startswith(prefix))
				# The request-id sanitiser has to round-trip it...
				self.assertTrue(is_id_safe(generated), generated)
				# ...and the fleet wildcard only covers ONE label below the zone.
				self.assertTrue(is_label_under(f"{generated}.{ZONE}", ZONE), generated)

	def test_a_region_that_is_not_a_label_is_slugged_into_one(self):
		# Region doc names are AWS codes today, but nothing forces that — and a space or an
		# underscore would otherwise reach a DNS record.
		self.assertEqual(name(self.series, region="AP South 1"), "gw1-ap-south-1")


class TestDeploymentNaming(unittest.TestCase):
	"""The name is what a list row reads as AND the engine's container name (`vllm-<name>`), so it
	has to say model, region and box, and stay one safe token."""

	def setUp(self):
		self.series = FakeSeries()

	def name(self, model="frappe/qwen3-8b", server="inf3-ap-south-1", region="ap-south-1"):
		return next_replica_name(model, server, region, counter=self.series)

	def test_the_name_says_what_where_and_which_box(self):
		self.assertEqual(self.name(), "qwen3-8b-ap-south-1-inf3-00001")

	def test_the_provider_prefix_is_not_repeated(self):
		# Every model of ours is `frappe/...` — the prefix separates nothing inside a deployment.
		self.assertEqual(self.name(model="acme/llama-3.1-8b"), "llama-3.1-8b-ap-south-1-inf3-00001")

	def test_the_box_keeps_only_its_own_part_of_its_name(self):
		# inf3-ap-south-1 already ends in the region, which the name carries once already.
		self.assertEqual(
			self.name(server="inf12-us-east-1", region="us-east-1"), "qwen3-8b-us-east-1-inf12-00001"
		)

	def test_a_region_code_goes_in_as_the_provider_writes_it(self):
		# No shortening rule: AWS, GCP and Azure each code a region differently, and one fitted
		# to AWS turns `asia-south1` and `southeastasia` into noise.
		for region in ("asia-south1", "southeastasia", "EU-RO-1"):
			with self.subTest(region):
				self.assertIn(slugify(region), self.name(region=region))

	def test_a_colo_box_in_no_region_simply_has_no_region_part(self):
		self.assertEqual(self.name(server="inf1", region=None), "qwen3-8b-inf1-00001")

	def test_the_number_is_what_keeps_two_of_the_same_apart(self):
		# One model twice on one box is legitimate — a second engine, its own port and cards.
		self.assertEqual(
			[self.name(), self.name()],
			["qwen3-8b-ap-south-1-inf3-00001", "qwen3-8b-ap-south-1-inf3-00002"],
		)

	def test_one_counter_serves_every_deployment(self):
		# Keyed as the old `MD-{#####}` format was, so the numbers carry on from what exists
		# rather than restarting onto names already taken.
		self.name()
		self.name(model="frappe/llama-70b", server="inf9-us-east-1", region="us-east-1")
		self.assertEqual(list(self.series.current), ["MD-"])

	def test_the_name_is_safe_as_a_container_name(self):
		# It reaches Docker as `vllm-<name>` and nginx as /e/<name> — the deployment's own
		# `_instance_slug` would otherwise rewrite it and the doc would name a container that
		# is not the one running. Dots are legal there, and model ids carry them.
		generated = self.name(model="acme/Qwen3.5 Coder")
		self.assertEqual(generated, "qwen3.5-coder-ap-south-1-inf3-00001")
		self.assertRegex(generated, r"^[a-z0-9._-]+$")


if __name__ == "__main__":
	unittest.main()
