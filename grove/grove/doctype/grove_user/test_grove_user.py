# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""One policy per login, and a login for every policy.

An email per test: IntegrationTestCase rolls back once when the class is done, not between
tests, so anything registered here is still there for the next one."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.grove_user.grove_user import GROVE_USER_ROLE, register_user


class IntegrationTestGroveUser(IntegrationTestCase):
	def test_a_policy_registers_the_login_it_names(self):
		# Provisioning is by email, for someone who may never have signed in.
		email = "grove-probe-new@example.com"
		self.assertFalse(frappe.db.exists("User", email))
		frappe.get_doc({"doctype": "Grove User", "user": register_user(email, "Probe Person")}).insert()
		self.assertEqual(frappe.db.get_value("User", email, "first_name"), "Probe Person")
		# Carries the Grove User role: an identity to scope later, no perms now.
		self.assertIn(GROVE_USER_ROLE, frappe.get_roles(email))

	def test_registering_twice_leaves_the_login_alone(self):
		email = "grove-probe-twice@example.com"
		register_user(email, "Probe Person")
		register_user(email, "Renamed")
		self.assertEqual(frappe.db.get_value("User", email, "first_name"), "Probe Person")

	def test_a_login_cannot_hold_two_policies(self):
		# The budget is the person's, so a second policy for one login is a second allowance.
		email = "grove-probe-twin@example.com"
		frappe.get_doc({"doctype": "Grove User", "user": register_user(email)}).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "Grove User", "user": email}).insert()
