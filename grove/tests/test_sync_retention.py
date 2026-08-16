# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Clearing old sync runs. Hits the DB — the whole point is that the child rows go too.

Agent Sync grows on a timer, not on use: two scheduled runs each write a doc every two minutes
whether or not anything moved. Left alone that is tens of thousands of docs inside the retention
window, each with a row per box and a payload on each row.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from grove.grove.doctype.agent_sync.agent_sync import AgentSync


class TestClearOldLogs(IntegrationTestCase):
	def run_aged(self, days_old, server="gw-retention-test"):
		"""One sync run with a child row, backdated by `days_old`."""
		doc = frappe.get_doc({
			"doctype": "Agent Sync",
			"run_at": now_datetime(),
			"sync_type": "Projection",
			"trigger": "Manual",
			"status": "Success",
			"targets_total": 1,
			"targets_ok": 1,
			"results": [{"server_type": "Gateway Server", "server": server, "success": 1}],
		}).insert(ignore_permissions=True, ignore_links=True)
		aged = add_days(now_datetime(), -days_old)
		frappe.db.set_value("Agent Sync", doc.name, "creation", aged, update_modified=False)
		return doc.name

	def rows_of(self, name):
		return frappe.db.count("Agent Sync Row", {"parent": name, "parenttype": "Agent Sync"})

	def test_a_run_past_the_window_goes(self):
		old = self.run_aged(90)
		self.assertTrue(self.rows_of(old))
		AgentSync.clear_old_logs(60)
		self.assertFalse(frappe.db.exists("Agent Sync", old))

	def test_its_child_rows_go_with_it(self):
		# The thing that rots quietly: delete the parent by itself and the rows stay forever,
		# orphaned and invisible, which is most of the table's size now that each carries a payload.
		old = self.run_aged(90)
		AgentSync.clear_old_logs(60)
		self.assertEqual(self.rows_of(old), 0)

	def test_a_recent_run_is_left_alone(self):
		recent = self.run_aged(1)
		AgentSync.clear_old_logs(60)
		self.assertTrue(frappe.db.exists("Agent Sync", recent))
		self.assertTrue(self.rows_of(recent))

	def test_the_window_is_what_the_caller_says(self):
		# Log Settings owns the number; the default is only a seed.
		run = self.run_aged(30)
		AgentSync.clear_old_logs(60)
		self.assertTrue(frappe.db.exists("Agent Sync", run))
		AgentSync.clear_old_logs(7)
		self.assertFalse(frappe.db.exists("Agent Sync", run))

	def test_it_is_wired_up_for_log_settings(self):
		# Without the hook, Log Settings refuses the doctype and nothing ever clears.
		self.assertEqual(frappe.get_hooks("default_log_clearing_doctypes", {}).get("Agent Sync"), [60])
