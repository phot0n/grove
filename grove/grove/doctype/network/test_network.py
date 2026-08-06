# Copyright (c) 2026, Grove and contributors
# See license.txt
"""security_group_ids parsing (pure) and cidr_block auto-assignment (hits the DB)."""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.network.network import (
	INFERENCE_INGRESS_RULES,
	PROXY_INGRESS_RULES,
	parse_security_group_ids,
)


class TestIngressRules(unittest.TestCase):
	"""What Grove opens to 0.0.0.0/0 on a box it builds. Nothing at runtime notices a rule that
	is wider than it needs to be — a widened group is invisible until somebody scans the box."""

	def ports(self, rules):
		return {(rule["from_port"], rule["to_port"]) for rule in rules}

	def test_an_inference_box_opens_only_ssh_and_its_engine_proxy(self):
		# Every vLLM instance is behind nginx under /e/<slug>/, and the exporters under
		# /metrics/* — so a box serving ten models opens no more than one serving a single model.
		# No 80: the front is TLS-only and nothing on the box listens in the clear.
		self.assertEqual(self.ports(INFERENCE_INGRESS_RULES), {(22, 22), (443, 443)})

	def test_no_engine_port_is_reachable_from_outside(self):
		# The old rule was the fixed range 8080-8085, written as a tuple, which _assign_engine_port
		# could outgrow: the seventh deployment on a box provisioned green and was unreachable.
		for rule in INFERENCE_INGRESS_RULES:
			with self.subTest(rule["from_port"]):
				self.assertIsInstance(rule["from_port"], int)
				self.assertFalse(rule["from_port"] <= 8080 <= rule["to_port"])

	def test_a_proxy_box_still_serves_the_client_api(self):
		self.assertEqual(self.ports(PROXY_INGRESS_RULES), {(22, 22), (80, 80), (443, 443)})


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
	"""The address plan is derived, not typed: an operator picks a provider and a region and
	Grove carves out the VPC range and the subnet inside it."""

	def setUp(self):
		self.provider = frappe.get_doc({
			"doctype": "Cloud Provider", "name": "test-network-cidr-provider", "provider_type": "aws",
			"api_key": "dummy-secret", "access_key_id": "dummy-key",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.provider.delete, ignore_permissions=True)

		self.region = frappe.get_doc({
			"doctype": "Region", "name": "test-network-cidr-region", "label": "CIDR test",
			"cloud_provider": "aws",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.region.delete, ignore_permissions=True)

	def make_network(self, name, cidr_block=None):
		network = frappe.get_doc({
			"doctype": "Network", "name": name, "region": self.region.name,
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

	def test_subnet_is_the_first_slash_24_of_the_vpc(self):
		network = self.make_network("test-cidr-subnet", cidr_block="10.5.0.0/16")
		self.assertEqual(network.subnet_cidr_block, "10.5.0.0/24")

	def test_bare_metal_placeholder_stays_blank(self):
		# No Cloud Provider → nothing is carved out, but a Region is still named: it is what a
		# Machine on this Network inherits.
		region = frappe.get_doc({
			"doctype": "Region", "name": "test-cidr-onprem-region", "label": "On-prem",
		}).insert(ignore_permissions=True)
		self.addCleanup(region.delete, ignore_permissions=True)

		network = frappe.get_doc({
			"doctype": "Network", "name": "test-cidr-placeholder", "region": region.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(network.delete, ignore_permissions=True)
		self.assertFalse(network.cidr_block)
		self.assertFalse(network.subnet_cidr_block)


if __name__ == "__main__":
	unittest.main()
