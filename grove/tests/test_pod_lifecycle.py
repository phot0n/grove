"""Spawn retries, the due-time tick and the Pod Activity rows every lifecycle call leaves.

Pure unit tests in the test_runpod style: a SimpleNamespace pod, a fake client, and the
provisioner's own state writes captured."""

import datetime
import json
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from grove.cloud_provider import provisioner, schedule
from grove.cloud_provider.provisioner import PodProvisioner
from grove.cloud_provider.runpod import RunPodError
from grove.grove.doctype.pod_activity import pod_activity

T0 = datetime.datetime(2026, 9, 8, 12, 0, 0)
DOCTYPE = pathlib.Path(__file__).parent.parent / "grove/doctype/pod_activity/pod_activity.json"


class Harness:
	"""One provisioner run with everything outside the lifecycle stubbed and recorded."""

	def __init__(self, creates, retries=2, pod_id="", stored_pod_id="p1"):
		self.pod = SimpleNamespace(name="POD-1", pod_id=pod_id, provision_retries=retries)
		self.provisioner = PodProvisioner(self.pod)
		self.creates, self.calls, self.writes, self.rows, self.sleeps = list(creates), [], [], [], []
		self.provisioner._client = SimpleNamespace(
			spawn_pod=self.spawn_pod,
			terminate_pod=lambda pod_id: self.calls.append(("terminate", pod_id)),
			stop_pod=self.stop_pod,
			get_pod=lambda pod_id: (_ for _ in ()).throw(RunPodError("RunPod GET /pods/x → 404: gone")),
		)
		self.stored_pod_id = stored_pod_id
		self.stop_error = None

	def spawn_pod(self, **kwargs):
		self.calls.append(("create", kwargs))
		outcome = self.creates.pop(0)
		if isinstance(outcome, Exception):
			raise outcome
		return outcome

	def stop_pod(self, pod_id):
		if self.stop_error:
			raise self.stop_error
		self.calls.append(("stop", pod_id))

	def run(self, action, **overrides):
		db = frappe._dict(get_value=lambda *args, **kwargs: self.stored_pod_id)
		stubs = dict(
			set_state=lambda p, values: self.writes.append(values),
			spawn_kwargs={},
			await_ready=lambda p: {"public_ip": "1.2.3.4"},
			current_status="Running",
			sync_model_published=lambda p: None,
			log=lambda p, *args, **kwargs: self.rows.append((args, kwargs)),
		)
		stubs.update(overrides)
		with (
			patch.multiple(PodProvisioner, **stubs),
			patch.object(provisioner.time, "sleep", side_effect=self.sleeps.append),
			patch.object(provisioner.frappe, "db", db),
			patch.object(provisioner.frappe, "log_error"),
			patch.object(provisioner.failure, "report") as reported,
			patch.object(frappe.utils, "now_datetime", lambda: T0),
		):
			self.reported = reported
			return getattr(self.provisioner, action)()

	def events(self):
		return [(args[0], args[1], kwargs.get("attempt")) for args, kwargs in self.rows]


class TestSpawnRetries(unittest.TestCase):
	def test_a_refusal_is_asked_again_up_to_the_retry_count(self):
		h = Harness([RunPodError("no capacity"), RunPodError("no capacity"), {"pod_id": "p1"}])
		result = h.run("spawn")
		self.assertEqual(result["status"], "success")
		self.assertEqual([c[0] for c in h.calls], ["create", "create", "create"])
		self.assertEqual(h.sleeps, [provisioner.SPAWN_RETRY_DELAY] * 2)
		self.assertEqual(
			[w.get("provision_attempts") for w in h.writes if "provision_attempts" in w], [0, 1, 2, 3]
		)
		self.assertIn({"pod_id": "p1"}, h.writes)
		self.assertEqual(
			h.events(), [("Spawn", "Failure", 1), ("Spawn", "Failure", 2), ("Spawn", "Success", None)]
		)
		self.assertEqual(h.rows[0][0][2].args, ("no capacity",))

	def test_the_last_refusal_gives_up_and_parks_the_pod(self):
		h = Harness([RunPodError("no capacity")] * 2, retries=1, stored_pod_id="")
		result = h.run("spawn", await_ready=lambda p: (_ for _ in ()).throw(AssertionError("must not run")))
		self.assertEqual(result["status"], "error")
		self.assertEqual(len([c for c in h.calls if c[0] == "create"]), 2)
		self.assertEqual(h.reported.call_count, 1)
		self.assertIn({"status": "Stopped"}, h.writes)
		# Two attempt rows from the loop, then the terminal one from fail().
		self.assertEqual(h.events(), [("Spawn", "Failure", 1), ("Spawn", "Failure", 2), ("Spawn", "Failure", None)])

	def test_zero_retries_means_one_ask(self):
		h = Harness([RunPodError("no capacity")], retries=0, stored_pod_id="")
		h.run("spawn")
		self.assertEqual(len(h.calls), 1)
		self.assertEqual(h.sleeps, [])

	def test_a_pod_that_got_an_id_is_never_created_twice(self):
		h = Harness([{"pod_id": "p1"}, {"pod_id": "p2"}])
		result = h.run("spawn", await_ready=lambda p: (_ for _ in ()).throw(RunPodError("poll timed out")))
		self.assertEqual(result["status"], "error")
		self.assertEqual(len([c for c in h.calls if c[0] == "create"]), 1)
		self.assertNotIn({"status": "Stopped"}, h.writes)  # it has a pod; reconcile owns its status
		self.assertEqual(h.events(), [("Spawn", "Failure", None)])


class TestTerminateAndTheRest(unittest.TestCase):
	def test_terminate_writes_once_and_logs_it(self):
		h = Harness([], pod_id="p1")
		self.assertEqual(h.run("terminate"), {"status": "success"})
		self.assertIn({"pod_id": "", "status": "Terminated", "engine_url": ""}, h.writes)
		self.assertEqual(h.events(), [("Terminate", "Success", None)])

	def test_a_pod_gone_on_the_provider_is_logged_as_terminated(self):
		h = Harness([], pod_id="p1")
		self.assertEqual(h.run("sync"), {"status": "Terminated"})
		self.assertIn({"pod_id": "", "status": "Terminated", "engine_url": ""}, h.writes)
		self.assertEqual(h.events(), [("Terminate", "Success", None)])
		self.assertIn("outside Grove", h.rows[0][0][2])

	def test_a_provider_error_on_terminate_is_a_failure_row(self):
		h = Harness([], pod_id="p1")
		h.provisioner._client.terminate_pod = lambda pod_id: (_ for _ in ()).throw(RunPodError("boom"))
		self.assertEqual(h.run("terminate")["status"], "error")
		self.assertEqual(h.events(), [("Terminate", "Failure", None)])

	def test_a_failed_stop_is_reported_not_raised(self):
		h = Harness([], pod_id="p1")
		h.stop_error = RunPodError("boom")
		self.assertEqual(h.run("stop")["status"], "error")
		self.assertEqual(h.reported.call_count, 1)
		self.assertEqual(h.events(), [("Stop", "Failure", None)])

	def test_a_stop_that_works_is_logged(self):
		h = Harness([], pod_id="p1")
		self.assertEqual(h.run("stop"), {"status": "Stopped"})
		self.assertEqual(h.events(), [("Stop", "Success", None)])


class TestTheRowItself(unittest.TestCase):
	def test_record_builds_one_committed_row(self):
		inserted, kwargs_seen = {}, {}

		def get_doc(values):
			inserted.update(values)
			return SimpleNamespace(insert=lambda **kwargs: kwargs_seen.update(kwargs))

		db = frappe._dict(commit=MagicMock())
		with (
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(frappe, "db", db),
			patch.object(frappe.utils, "now_datetime", lambda: T0 + datetime.timedelta(seconds=90)),
		):
			pod_activity.record(
				"POD-1", "Spawn", "Failure", RunPodError("no capacity"), attempt=2, started=T0, trigger="Scheduled"
			)
		self.assertEqual(inserted["doctype"], "Pod Activity")
		self.assertEqual((inserted["pod"], inserted["event"], inserted["outcome"]), ("POD-1", "Spawn", "Failure"))
		self.assertEqual((inserted["attempt"], inserted["trigger"], inserted["duration"]), (2, "Scheduled", 90.0))
		self.assertEqual(inserted["detail"], "no capacity")
		self.assertEqual(kwargs_seen, {"ignore_permissions": True})
		self.assertTrue(db.commit.called)

	def test_the_events_the_provisioner_records_are_the_doctypes_options(self):
		options = json.loads(DOCTYPE.read_text())
		event = next(f for f in options["fields"] if f["fieldname"] == "event")["options"].split("\n")
		source = pathlib.Path(provisioner.__file__).read_text()
		recorded = {name for name in ("Spawn", "Restart", "Stop", "Start", "Terminate") if f'"{name}"' in source}
		self.assertEqual(set(event), recorded)


class TestTheWindow(unittest.TestCase):
	def test_a_same_day_window(self):
		w = schedule.Window(datetime.time(9), datetime.time(18))
		self.assertTrue(w.contains(datetime.datetime(2026, 9, 8, 9, 0)))
		self.assertTrue(w.contains(datetime.datetime(2026, 9, 8, 17, 59)))
		self.assertFalse(w.contains(datetime.datetime(2026, 9, 8, 18, 0)))
		self.assertFalse(w.contains(datetime.datetime(2026, 9, 8, 8, 59)))
		self.assertEqual(w.started_on(datetime.datetime(2026, 9, 8, 12, 0)), datetime.date(2026, 9, 8))

	def test_a_window_across_midnight(self):
		w = schedule.Window(datetime.time(22), datetime.time(6))
		self.assertTrue(w.contains(datetime.datetime(2026, 9, 8, 23, 0)))
		self.assertTrue(w.contains(datetime.datetime(2026, 9, 9, 5, 59)))
		self.assertFalse(w.contains(datetime.datetime(2026, 9, 9, 6, 0)))
		self.assertFalse(w.contains(datetime.datetime(2026, 9, 9, 12, 0)))
		# The early-morning half belongs to the day the window opened.
		self.assertEqual(w.started_on(datetime.datetime(2026, 9, 9, 3, 0)), datetime.date(2026, 9, 8))
		self.assertEqual(w.started_on(datetime.datetime(2026, 9, 8, 23, 0)), datetime.date(2026, 9, 8))

	def test_the_database_shape_and_the_form_shape_both_read(self):
		self.assertEqual(schedule.as_time(datetime.timedelta(hours=9, minutes=30)), datetime.time(9, 30))
		self.assertEqual(schedule.as_time("09:30:00"), datetime.time(9, 30))
		self.assertEqual(schedule.as_time(datetime.time(9, 30)), datetime.time(9, 30))


class TestTheWindowTick(unittest.TestCase):
	def tick(self, pods, now=datetime.datetime(2026, 9, 8, 12, 0)):
		writes, queued = [], []
		db = frappe._dict(set_value=lambda *args: writes.append(args), commit=lambda: None, rollback=lambda: None)
		rows = [frappe._dict(pod) for pod in pods]
		with (
			patch.object(schedule.frappe, "get_all", return_value=rows),
			patch.object(schedule.frappe, "db", db),
			patch.object(schedule.frappe, "enqueue", side_effect=lambda *a, **k: queued.append((a[0], k))),
			patch.object(schedule.frappe, "log_error") as logged,
			patch.object(frappe.utils, "now_datetime", lambda: now),
		):
			schedule.run_due_pods()
		return writes, queued, logged

	def pod(self, **over):
		base = dict(name="POD-1", provision_at=datetime.timedelta(hours=9), terminate_at=datetime.timedelta(hours=18),
			pod_id="", status="Pending", scheduled_for=None)
		return {**base, **over}

	def test_inside_the_window_an_unprovisioned_pod_is_spawned_once_a_day(self):
		writes, queued, _ = self.tick([self.pod()])
		self.assertEqual(writes, [("Pod", "POD-1", "scheduled_for", datetime.date(2026, 9, 8))])
		method, kwargs = queued[0]
		self.assertEqual(method, "grove.cloud_provider.provisioner.spawn_pod_doc")
		self.assertEqual((kwargs["pod_name"], kwargs["trigger"], kwargs["job_id"], kwargs["deduplicate"]),
			("POD-1", "Scheduled", "pod-spawn_pod_doc-POD-1", True))

	def test_a_window_already_opened_today_is_not_reopened(self):
		# Covers both a spawn that gave up and a Stop pressed by hand inside the window.
		writes, queued, _ = self.tick([self.pod(status="Stopped", scheduled_for=datetime.date(2026, 9, 8))])
		self.assertEqual((writes, queued), ([], []))

	def test_yesterdays_window_does_not_count_today(self):
		_, queued, _ = self.tick([self.pod(status="Terminated", scheduled_for=datetime.date(2026, 9, 7))])
		self.assertEqual(len(queued), 1)

	def test_outside_the_window_a_live_pod_is_terminated(self):
		writes, queued, _ = self.tick([self.pod(pod_id="p1", status="Running")], now=datetime.datetime(2026, 9, 8, 18, 0))
		self.assertEqual(writes, [])
		self.assertEqual(queued[0][0], "grove.cloud_provider.provisioner.terminate_pod_doc")
		self.assertEqual(queued[0][1]["job_id"], "pod-terminate_pod_doc-POD-1")

	def test_outside_the_window_nothing_else_happens(self):
		_, queued, _ = self.tick([self.pod(pod_id="", status="Terminated")], now=datetime.datetime(2026, 9, 8, 20, 0))
		self.assertEqual(queued, [])

	def test_one_bad_pod_does_not_stop_the_tick(self):
		bad = self.pod(name="POD-0", provision_at="not a time")
		_, queued, logged = self.tick([bad, self.pod()])
		self.assertEqual(logged.call_count, 1)
		self.assertEqual(len(queued), 1)


class TestPodValidation(unittest.TestCase):
	def test_half_a_window_or_a_zero_length_one_is_refused(self):
		from grove.grove.doctype.pod.pod import validate_window

		with patch.object(frappe, "throw", side_effect=ValueError):
			with self.assertRaises(ValueError):
				validate_window(SimpleNamespace(provision_at="09:00:00", terminate_at=None))
			with self.assertRaises(ValueError):
				validate_window(SimpleNamespace(provision_at="09:00:00", terminate_at="09:00:00"))
			validate_window(SimpleNamespace(provision_at=None, terminate_at=None))
			validate_window(SimpleNamespace(provision_at="22:00:00", terminate_at="06:00:00"))
