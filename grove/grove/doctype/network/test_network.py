# Copyright (c) 2026, Grove and contributors
# See license.txt
"""security_group_ids parsing (pure) and cidr_block auto-assignment (hits the DB)."""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.network.network import parse_security_group_ids


class TestParseSecurityGroupIds(unittest.TestCase):
	def test_splits_on_comma(self):
		self.assertEqual(parse_security_group_ids("sg-abc,sg-def"), ["sg-abc", "sg-def"])

	def test_strips_whitespace(self):
		self.assertEqual(parse_security_group_ids("sg-abc, sg-def , sg-ghi"),
						  ["sg-abc", "sg-def", "sg-ghi"])

	def test_blank_is_empty_list(self):
		self.assertEqual(parse_security_group_ids(""), [])
		self.assertEqual(parse_security_group_ids(None), [])

	def test_drops_empty_entries(self):
		self.assertEqual(parse_security_group_ids("sg-abc,,sg-def,"), ["sg-abc", "sg-def"])


class TestCidrBlockAutoAssignment(IntegrationTestCase):
	def setUp(self):
		self.provider = frappe.get_doc({
			"doctype": "Cloud Provider", "name": "test-network-cidr-provider", "provider_type": "aws",
			"api_key": "dummy-secret", "access_key_id": "dummy-key",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.provider.delete, ignore_permissions=True)

	def make_network(self, name, cidr_block=None):
		network = frappe.get_doc({
			"doctype": "Network", "name": name,
			"cloud_provider": self.provider.name, "cidr_block": cidr_block,
		}).insert(ignore_permissions=True)
		self.addCleanup(network.delete, ignore_permissions=True)
		return network

	def test_assigns_an_unused_block(self):
		used_before = {block for block in frappe.get_all("Network", pluck="cidr_block") if block}
		network = self.make_network("test-cidr-first")
		self.assertTrue(network.cidr_block, "no block was assigned")
		self.assertNotIn(network.cidr_block, used_before)

	def test_skips_blocks_already_taken_by_another_network(self):
		taken = self.make_network("test-cidr-a").cidr_block
		network_b = self.make_network("test-cidr-b")
		self.assertNotEqual(network_b.cidr_block, taken)

	def test_does_not_overwrite_a_manually_set_block(self):
		network = self.make_network("test-cidr-manual", cidr_block="10.5.0.0/16")
		self.assertEqual(network.cidr_block, "10.5.0.0/16")

	def test_bare_metal_placeholder_stays_blank(self):
		network = frappe.get_doc({
			"doctype": "Network", "name": "test-cidr-placeholder",
		}).insert(ignore_permissions=True)
		self.addCleanup(network.delete, ignore_permissions=True)
		self.assertFalse(network.cidr_block)


if __name__ == "__main__":
	unittest.main()
