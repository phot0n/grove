# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Where an agent pushes. Its Region's own endpoint when that Region has one — sending a
region's samples across the world to a single ingestion URL is what regional endpoints exist
to avoid — and the fleet-wide URL when it does not."""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

FALLBACK_URL = "http://fleet-wide.example.com/v1/write"
REGIONAL_URL = "http://ap-south-1.example.com/v1/write"


class TestRemoteWriteUrl(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single("Grove Settings")
		settings.metrics_remote_write_url = FALLBACK_URL
		settings.save(ignore_permissions=True)

	def agent(self, name, remote_write_url):
		"""An agent on a box in its own Region, which may or may not name an endpoint."""
		region = frappe.get_doc({
			"doctype": "Region", "name": f"test-agent-region-{name}",
			"label": name, "remote_write_url": remote_write_url,
		}).insert(ignore_permissions=True)
		self.addCleanup(region.delete, ignore_permissions=True)

		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": f"test-agent-box-{name}", "region": region.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)

		agent = frappe.get_doc({
			"doctype": "Monitoring Agent", "name": f"test-agent-{name}", "machine": machine.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(agent.delete, ignore_permissions=True)
		return agent

	def test_a_regions_own_endpoint_wins(self):
		self.assertEqual(self.agent("regional", REGIONAL_URL).remote_write_url, REGIONAL_URL)

	def test_a_region_without_one_falls_back_to_the_fleet_wide_url(self):
		self.assertEqual(self.agent("fallback", "").remote_write_url, FALLBACK_URL)

	def test_the_resolved_url_is_what_ansible_is_given(self):
		agent = self.agent("vars", REGIONAL_URL)
		self.assertEqual(agent.ansible_variables["monitoring_remote_write_url"], REGIONAL_URL)


if __name__ == "__main__":
	unittest.main()
