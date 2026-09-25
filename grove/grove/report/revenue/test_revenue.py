"""Revenue is the day rows priced at today's tables, cut by model, key, user or day."""

from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.grove_user.grove_user import register_user
from grove.grove.report.revenue.revenue import execute
from grove.utils import utc_today


class TestRevenue(IntegrationTestCase):
	"""One model at 10 USD/Mtok of completion: 100 000 tokens today, 200 000 forty days ago."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.today = utc_today()
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "revenue-7b", "modality": "text", "hf_repo": "org/revenue-7b"}
		).insert(ignore_permissions=True).name
		pricing = frappe.get_doc({
			"doctype": "Model Pricing", "model": cls.model, "status": "Enabled",
			"rates": [{"counter": "completion_tokens", "rate": 10}],
		}).insert(ignore_permissions=True)
		# Enabled today would price nothing before today; the old day needs a window that covers it.
		frappe.db.set_value("Model Pricing", pricing.name, "enabled_on", cls.today - timedelta(days=60))
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("revenue@grove.test"), "free": 1}
		).insert(ignore_permissions=True).name
		cls.key = frappe.get_doc({"doctype": "Grove API Key", "user": cls.user}).insert(ignore_permissions=True).name
		cls.other_key = frappe.get_doc({"doctype": "Grove API Key", "user": cls.user}).insert(ignore_permissions=True).name
		cls.record(cls.key, cls.today, completion=100_000, requests=2)
		cls.record(cls.key, cls.today - timedelta(days=40), completion=200_000, requests=1)

	@classmethod
	def record(cls, key, day, completion, requests):
		frappe.get_doc({
			"doctype": "Usage Record", "api_key": key, "user": cls.user, "day": day,
			"counter_usage": [
				{"model": cls.model, "counter": "completion_tokens", "amount": completion},
				{"model": cls.model, "counter": "request_count", "amount": requests},
			],
		}).insert(ignore_permissions=True)

	def report(self, group_by="Model", days=30, **filters):
		_, rows = execute({
			"from_date": self.today - timedelta(days=days), "to_date": self.today, "group_by": group_by,
			"grove_user": self.user, **filters,
		})
		return [(row["label"], row["requests"], row["revenue"]) for row in rows]

	def test_the_last_month_by_model(self):
		self.assertEqual(self.report(), [(self.model, 2, 1.0)])

	def test_by_key_user_and_day(self):
		self.assertEqual(self.report("API Key"), [(self.key, 2, 1.0)])
		self.assertEqual(self.report("Grove User"), [(self.user, 2, 1.0)])
		self.assertEqual(self.report("Day"), [(self.today, 2, 1.0)])

	def test_a_wider_range_adds_the_old_day(self):
		self.assertEqual(self.report(days=60), [(self.model, 3, 3.0)])
		self.assertEqual(
			self.report("Day", days=60), [(self.today - timedelta(days=40), 1, 2.0), (self.today, 2, 1.0)]
		)

	def test_a_key_filter_narrows(self):
		self.assertEqual(self.report(api_key=self.other_key), [])
		self.assertEqual(self.report("API Key", days=60, api_key=self.key), [(self.key, 3, 3.0)])
