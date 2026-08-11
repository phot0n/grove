# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What happens when a background job fails.

The bug these exist for: a Gateway Server provision set status to Installing, committed, then raised
on a missing password. No Ansible Play was ever created, nothing was written to the doc, and the
status stayed Installing forever — the only record was the RQ job's traceback, which someone had to
know to go looking for.

Pure. `frappe.local` and the four reporting calls are patched, so no site and no worker.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grove import failure


class Recorder:
	"""Stands in for every side effect `report` has, so a test can assert on what was announced."""

	def __init__(self):
		self.comments, self.toasts, self.notifications, self.statuses = [], [], [], []

	def patches(self):
		return (
			patch.object(failure, "_comment", lambda dt, n, m: self.comments.append((dt, n, m))),
			patch.object(failure, "_toast", lambda dt, n, t, d: self.toasts.append((dt, n, t, d))),
			patch.object(failure, "_notify", lambda dt, n, t, d: self.notifications.append((dt, n, t, d))),
			patch.object(failure, "_mark_broken", lambda dt, n: self.statuses.append((dt, n))),
		)

	def __enter__(self):
		self._active = [p for p in self.patches()]
		for p in self._active:
			p.start()
		self._local = patch.object(failure.frappe, "local", SimpleNamespace())
		self._local.start()
		return self

	def __exit__(self, *exc):
		self._local.stop()
		for p in self._active:
			p.stop()


class TestReportReachesEveryAudience(unittest.TestCase):
	"""Three channels because they miss each other: the toast reaches whoever is still on the page,
	the notification reaches them after they close it, and the comment is what is still there next
	week when someone asks what happened to this box."""

	def test_a_failure_is_announced_three_ways(self):
		with Recorder() as rec:
			failure.report("Gateway Server", "gw-1", "Provision failed", "no admin token")
		self.assertEqual(1, len(rec.comments))
		self.assertEqual(1, len(rec.toasts))
		self.assertEqual(1, len(rec.notifications))

	def test_the_comment_says_what_went_wrong(self):
		with Recorder() as rec:
			failure.report("Gateway Server", "gw-1", "Provision failed", "no admin token")
		_, _, message = rec.comments[0]
		self.assertIn("Provision failed", message)
		self.assertIn("no admin token", message)

	def test_a_play_with_no_reference_doc_is_skipped(self):
		# Nothing to comment on and nothing to link a notification to. The Error Log is the record
		# for that one — announcing it against a blank doc would be worse than not announcing it.
		with Recorder() as rec:
			failure.report(None, None, "Play failed", "detail")
			failure.report("Gateway Server", None, "Play failed", "detail")
		self.assertEqual([], rec.comments + rec.toasts + rec.notifications)

	def test_reporting_never_marks_broken_unless_asked(self):
		with Recorder() as rec:
			failure.report("Gateway Server", "gw-1", "Deploy agent failed", "boom")
		self.assertEqual([], rec.statuses)

	def test_marking_broken_is_opt_in(self):
		with Recorder() as rec:
			failure.report("Gateway Server", "gw-1", "Provision failed", "boom", mark_broken=True)
		self.assertEqual([("Gateway Server", "gw-1")], rec.statuses)


class TestTheDecoratorReportsAndReraises(unittest.TestCase):
	def failing_doc(self):
		return SimpleNamespace(doctype="Gateway Server", name="gw-1")

	def test_the_exception_still_reaches_the_worker(self):
		# Reporting is not rescuing. If this swallowed, RQ would mark the job successful and Frappe
		# would write no Error Log — the traceback would be gone entirely.
		@failure.reports_failure()
		def provision(self):
			raise ValueError("no admin token")

		with Recorder(), self.assertRaises(ValueError):
			provision(self.failing_doc())

	def test_it_reports_against_the_doc_it_was_called_on(self):
		@failure.reports_failure(mark_broken=True)
		def provision(self):
			raise ValueError("no admin token")

		with Recorder() as rec:
			with self.assertRaises(ValueError):
				provision(self.failing_doc())
		doctype, name, title, detail = rec.toasts[0]
		self.assertEqual(("Gateway Server", "gw-1"), (doctype, name))
		self.assertEqual("Provision failed", title)
		self.assertIn("no admin token", detail)
		self.assertEqual([("Gateway Server", "gw-1")], rec.statuses)

	def test_a_module_level_job_names_its_doctype(self):
		# The Model Deployment jobs are functions taking a docname, not methods with a self.
		@failure.reports_failure(doctype="Model Deployment")
		def deploy_model(model_deployment):
			raise ValueError("engine did not come up")

		with Recorder() as rec:
			with self.assertRaises(ValueError):
				deploy_model("MD-00007")
		doctype, name, title, _ = rec.toasts[0]
		self.assertEqual(("Model Deployment", "MD-00007"), (doctype, name))
		self.assertEqual("Deploy model failed", title)

	def test_a_leading_underscore_does_not_reach_the_operator(self):
		# _deploy_agent is what the method is called; "Deploy agent" is what the button says.
		@failure.reports_failure()
		def _deploy_agent(self):
			raise ValueError("boom")

		with Recorder() as rec:
			with self.assertRaises(ValueError):
				_deploy_agent(self.failing_doc())
		self.assertEqual("Deploy agent failed", rec.toasts[0][2])

	def test_success_is_silent(self):
		@failure.reports_failure(mark_broken=True)
		def provision(self):
			return "done"

		with Recorder() as rec:
			self.assertEqual("done", provision(self.failing_doc()))
		self.assertEqual([], rec.comments + rec.toasts + rec.notifications)
		self.assertEqual([], rec.statuses)

	def test_one_failure_is_announced_once(self):
		"""A failed play reports through AnsiblePlay.on_update, and the caller may then raise on the
		same failure. Same worker, same job, so frappe.local remembers and the second half stays
		quiet — one event, one notification."""

		@failure.reports_failure()
		def provision(self):
			failure.report("Gateway Server", "gw-1", "gateway.yml failed", "see the play")
			raise ValueError("rc was 1")

		with Recorder() as rec:
			with self.assertRaises(ValueError):
				provision(self.failing_doc())
		self.assertEqual(1, len(rec.toasts))
		self.assertEqual("gateway.yml failed", rec.toasts[0][2])


class TestMarkingBrokenIsNarrow(unittest.TestCase):
	"""The status write is the part that can do damage, so it refuses more than it accepts."""

	def run_mark(self, current_status, options="Pending\nInstalling\nActive\nBroken\nTerminated"):
		meta = MagicMock()
		meta.get_field.return_value = SimpleNamespace(options=options) if options else None
		db = MagicMock()
		db.get_value.return_value = current_status
		with patch.object(failure.frappe, "get_meta", return_value=meta), patch.object(failure.frappe, "db", db):
			failure._mark_broken("Gateway Server", "gw-1")
		return db.set_value.called

	def test_a_box_mid_install_is_marked_broken(self):
		# The actual bug: it sat at Installing forever, looking like a slow provision.
		self.assertTrue(self.run_mark("Installing"))
		self.assertTrue(self.run_mark("Provisioning"))

	def test_a_terminated_box_is_left_alone(self):
		# Torn down mid-play is gone, not broken — and calling it Broken would put it back in front
		# of anyone filtering for boxes to fix.
		self.assertFalse(self.run_mark("Terminated"))

	def test_an_already_active_box_is_left_alone(self):
		# A failed config push against a live box has not stopped it serving. Marking it Broken here
		# would drop it out of the route table over a failed deploy.
		self.assertFalse(self.run_mark("Active"))

	def test_a_doctype_with_nowhere_to_go_is_left_alone(self):
		# Machine and Pod have no Broken status at all.
		self.assertFalse(self.run_mark("Provisioning", options="Pending\nProvisioning\nActive\nTerminated"))
		self.assertFalse(self.run_mark("Provisioning", options=None))
