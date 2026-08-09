# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The one rule for which address reaches a box. Two callers depend on it agreeing with itself:
the metrics agent scrapes an address, and the security group opens a hole for the address it
expects to arrive from. They disagree only silently — a target that is permanently down, or a
firewall hole for nobody."""

import unittest

from grove.net import reachable_ip

NETWORK = "NET-mumbai"


def box(network="", private_ip="", ip="203.0.113.7"):
	return {"network": network, "private_ip": private_ip, "ip": ip}


class TestReachableIp(unittest.TestCase):
	def test_a_shared_network_is_reached_privately(self):
		self.assertEqual(
			reachable_ip(box(network=NETWORK, private_ip="10.0.0.5"), NETWORK), "10.0.0.5"
		)

	def test_another_network_is_reached_publicly(self):
		self.assertEqual(
			reachable_ip(box(network="NET-virginia", private_ip="10.0.0.5"), NETWORK), "203.0.113.7"
		)

	def test_a_viewer_in_no_network_reaches_publicly(self):
		# Its own box is unplaced, so it has no private route to anything.
		self.assertEqual(
			reachable_ip(box(network=NETWORK, private_ip="10.0.0.5"), ""), "203.0.113.7"
		)

	def test_a_shared_network_with_no_private_address_falls_back(self):
		# Colo and bare metal: in a Network on paper, no private address in fact.
		self.assertEqual(reachable_ip(box(network=NETWORK), NETWORK), "203.0.113.7")

	def test_a_pod_has_neither_and_is_reached_publicly(self):
		self.assertEqual(reachable_ip(box(), NETWORK), "203.0.113.7")

	def test_a_box_with_no_address_at_all_answers_nothing(self):
		# Callers skip these rather than emit a target or a hole for the empty string.
		self.assertIsNone(reachable_ip({}, NETWORK))
