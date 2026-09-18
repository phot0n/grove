"""The connections badge: what Frappe counts as open per doctype."""

import unittest

from grove import hooks, notifications


class TestTheConnectionsBadge(unittest.TestCase):
	def test_frappe_is_pointed_at_grove_config(self):
		self.assertEqual(hooks.notification_config, "grove.notifications.get_notification_config")

	def test_a_machine_is_open_while_active(self):
		# The Network form's Machine link then reads "N active" rather than "N ever".
		config = notifications.get_notification_config()
		self.assertEqual(config["for_doctype"]["Machine"], {"status": "Active"})

	def test_a_replica_is_open_while_serving(self):
		# The Model Deployment form's replica link then reads "N serving" rather than "N placed".
		config = notifications.get_notification_config()
		self.assertEqual(config["for_doctype"]["Model Replica"], {"status": "Active"})

	def test_a_pod_activity_is_open_while_failed(self):
		# The Pod form's Activity link then reads "N failed" rather than "N calls".
		config = notifications.get_notification_config()
		self.assertEqual(config["for_doctype"]["Pod Activity"], {"outcome": "Failure"})
