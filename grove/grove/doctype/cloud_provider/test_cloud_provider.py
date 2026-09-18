# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestCloudProvider(IntegrationTestCase):
	def test_resource_type_follows_the_provider(self):
		for provider_type, resource_type in (("runpod", "Pod"), ("aws", "Machine")):
			with self.subTest(provider_type):
				provider = frappe.get_doc({"doctype": "Cloud Provider", "provider_type": provider_type})
				provider.validate()
				self.assertEqual(provider.resource_type, resource_type)
