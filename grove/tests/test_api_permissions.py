# Copyright (c) 2026, Grove and contributors
# See license.txt
"""grove.api runs under permission checks, not around them: the reads go through
frappe.get_list and the writes through a plain save, so the Grove Control role has to carry
exactly what those endpoints touch — and nothing more. Site-backed: DocPerms are the
behaviour under test."""

import frappe
from frappe.tests import IntegrationTestCase

from grove import api
from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX

PROBE = "control-probe@example.com"
WITHHELD = ("Machine", "Inference Server", "Model Replica", "Monitoring Agent")


class TestTheControlRoleReachesOnlyWhatItServes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		"""One probe user for the class: IntegrationTestCase rolls back once at the end, not
		per test, so a per-test insert would collide with itself."""
		super().setUpClass()
		frappe.get_doc(
			{
				"doctype": "User",
				"email": PROBE,
				"first_name": "Control Probe",
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": api.CONTROL_ROLE}],
			}
		).insert()

	def setUp(self):
		frappe.set_user(PROBE)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_the_catalogue_and_the_usage_report_are_readable(self):
		self.assertIsInstance(api.available_models(), list)
		self.assertEqual(api.usage(["nobody@example.com"])["model_summary"], [])

	def test_usage_model_rows_resolve_permission_through_their_parent(self):
		# A child doctype holds no permissions of its own, so this is a PermissionError
		# without parent_doctype however the role is granted.
		frappe.get_list(
			"Usage Model Row",
			filters={"parenttype": "Usage Record"},
			fields=["model"],
			parent_doctype="Usage Record",
		)

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_a_key_registers_the_login_it_names(self):
		frappe.db.set_single_value("Grove Settings", "gateway_host", "gw.probe.test")
		result = api.provision_key("Probe Person", "probe-person@example.com", token_limit=99)

		self.assertTrue(result["api_key"].startswith(KEY_PREFIX))
		self.assertEqual(frappe.db.get_value("User", "probe-person@example.com", "first_name"), "Probe Person")
		grove_user = frappe.db.get_value("Grove User", {"user": "probe-person@example.com"}, "max_tokens")
		self.assertEqual(grove_user, 99)
