# Copyright (c) 2026, Grove and contributors
# See license.txt
"""How a server names itself. Pure — frappe's two reads are replaced.

The name is not a label, it is infrastructure: a Gateway Server's and an Ingress Server's name IS
their record under the fleet zone, and every server's name is a part of the request ids it stamps.
So the two properties worth pinning are that it stays one DNS label, and that a retired name is
never handed to a different box.
"""

import unittest
from unittest.mock import patch

import frappe

from grove.naming import next_server_name
from grove.utils import is_id_safe, is_label_under

ZONE = "grove.example.com"


def naming(existing, region="ap-south-1"):
	"""next_server_name against a fleet that already holds `existing` names."""
	return patch.multiple(
		frappe,
		db=frappe._dict(get_value=lambda *args, **kwargs: region),
		get_all=lambda *args, **kwargs: list(existing),
	)


def name(existing=(), region="ap-south-1", prefix="gw"):
	with naming(existing, region):
		return next_server_name("Gateway Server", prefix, "MACHINE-1")


class TestServerNaming(unittest.TestCase):
	def test_the_first_server_in_a_region_is_one(self):
		self.assertEqual(name(), "gw1-ap-south-1")

	def test_the_next_one_counts_up(self):
		self.assertEqual(name(["gw1-ap-south-1", "gw2-ap-south-1"]), "gw3-ap-south-1")

	def test_each_region_counts_on_its_own(self):
		# Otherwise the numbers race ahead of what anyone can say out loud, and gw7-ap-south-1
		# would mean "the seventh gateway anywhere" rather than the second one here.
		self.assertEqual(name(["gw1-us-east-1", "gw2-us-east-1"]), "gw1-ap-south-1")

	def test_another_kind_of_server_does_not_advance_the_count(self):
		self.assertEqual(name(["ing1-ap-south-1", "ing2-ap-south-1"]), "gw1-ap-south-1")

	def test_a_retired_name_is_never_handed_to_another_box(self):
		# Highest used plus one, not lowest free. A gone server may still have a DNS record or a
		# log history behind it, and reusing its name would quietly merge the two.
		self.assertEqual(name(["gw1-ap-south-1", "gw3-ap-south-1"]), "gw4-ap-south-1")

	def test_a_box_with_no_region_gets_no_suffix(self):
		# A colo machine is in no Network and so has no region — which is exactly what the name
		# should say, rather than inventing one.
		self.assertEqual(name(region=""), "gw1")
		self.assertEqual(name(["gw1", "gw2"], region=""), "gw3")

	def test_a_regionless_name_does_not_count_against_a_region(self):
		self.assertEqual(name(["gw1", "gw2"]), "gw1-ap-south-1")

	def test_a_hand_typed_name_is_left_out_of_the_count(self):
		# The fleet still holds gw-ap-south and mc-sg-proxy from before this existed. They match
		# no pattern, so they neither collide nor advance the number.
		self.assertEqual(name(["gw-ap-south", "mc-sg-proxy", ""]), "gw1-ap-south-1")

	def test_every_prefix_produces_a_usable_name(self):
		for prefix in ("gw", "ing", "inf"):
			with self.subTest(prefix):
				generated = name(prefix=prefix)
				self.assertTrue(generated.startswith(prefix))
				# The request-id sanitiser has to round-trip it...
				self.assertTrue(is_id_safe(generated), generated)
				# ...and the fleet wildcard only covers ONE label below the zone.
				self.assertTrue(is_label_under(f"{generated}.{ZONE}", ZONE), generated)

	def test_a_region_that_is_not_a_label_is_slugged_into_one(self):
		# Region doc names are AWS codes today, but nothing forces that — and a space or an
		# underscore would otherwise reach a DNS record.
		self.assertEqual(name(region="AP South 1"), "gw1-ap-south-1")


if __name__ == "__main__":
	unittest.main()
