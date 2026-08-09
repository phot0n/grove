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

from grove.naming import next_server_name
from grove.utils import is_id_safe, is_label_under

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


if __name__ == "__main__":
	unittest.main()
