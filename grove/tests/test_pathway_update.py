# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Pathway Update: its rows, Start / Continue / Stop, the deploy step and what a failure leaves
behind. Pure: frappe is patched and servers and rows are fakes, so no site and no box."""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import requests

from grove.grove.doctype.pathway_update import pathway_update as module
from grove.grove.doctype.pathway_update.pathway_update import PathwayUpdate
from grove.runner import Status, StepHandler


def setUpModule():
	patches = [
		# now_datetime reads the site's time zone.
		patch.object(module, "now_datetime", side_effect=datetime.now),
		patch.object(module, "gateway_agent_version", return_value="v0.0.4"),
		patch.object(module, "pathway_repo", return_value="someone/pathway"),
	]
	for patcher in patches:
		patcher.start()
		unittest.addModuleCleanup(patcher.stop)


class FakeRow(SimpleNamespace):
	def save(self):
		pass


def row(server_type, server, status=Status.Pending, **fields):
	return FakeRow(server_type=server_type, server=server, status=status, idx=0, **fields)


def update(**fields):
	doc = PathwayUpdate.__new__(PathwayUpdate)
	doc.__dict__.update(
		doctype="Pathway Update", name="u1", release="v0.0.4", repo="someone/pathway", status=Status.Pending,
		gateways=1, ingresses=1, servers=[], error=None, started_at=None, ended_at=None,
	)
	doc.__dict__.update(fields)
	doc.set = lambda field, rows: setattr(doc, field, list(rows))
	doc.append = lambda field, values: doc.servers.append(FakeRow(**values))
	doc.update = lambda values: doc.__dict__.update(values)
	doc.save = Mock()
	doc.enqueue_steps = Mock()
	return doc


def refused():
	return patch.object(frappe, "throw", side_effect=frappe.ValidationError)


class TestInsert(unittest.TestCase):
	def insert(self, doc, active):
		with patch.object(frappe, "get_all", side_effect=lambda doctype, **kwargs: active[doctype]) as get_all:
			PathwayUpdate.before_insert(doc)
		return get_all

	def test_the_release_and_repo_are_grove_settings(self):
		doc = update(release=None, repo=None, gateways=0)
		self.insert(doc, {"Ingress Server": []})
		self.assertEqual(("v0.0.4", "someone/pathway"), (doc.release, doc.repo))

	def test_each_ticked_box_adds_its_active_servers_ingresses_first(self):
		doc = update()
		get_all = self.insert(doc, {"Ingress Server": ["ing1"], "Gateway Server": ["gw1", "gw2"]})
		get_all.assert_any_call("Gateway Server", filters={"status": "Active"}, order_by="name asc", pluck="name")
		self.assertEqual(
			[("Ingress Server", "ing1"), ("Gateway Server", "gw1"), ("Gateway Server", "gw2")],
			[(r.server_type, r.server) for r in doc.servers],
		)

	def test_an_unticked_box_adds_nothing(self):
		doc = update(ingresses=0)
		self.insert(doc, {"Gateway Server": ["gw1"]})
		self.assertEqual(["gw1"], [r.server for r in doc.servers])

	def test_a_duplicate_starts_from_the_servers_active_now(self):
		doc = update(ingresses=0, servers=[row("Gateway Server", "gw-old", Status.Success)])
		self.insert(doc, {"Gateway Server": ["gw1"]})
		self.assertEqual(["gw1"], [r.server for r in doc.servers])

	def test_neither_box_ticked_is_refused(self):
		with refused(), self.assertRaises(frappe.ValidationError):
			self.insert(update(gateways=0, ingresses=0), {})


class TestRowsAddedByHand(unittest.TestCase):
	def test_a_new_row_is_made_a_pending_deploy(self):
		added = row("Gateway Server", "gw3", status=None)
		doc = update(servers=[added])
		PathwayUpdate.validate(doc)
		self.assertEqual((Status.Pending, "deploy_server"), (added.status, added.method_name))

	def test_a_type_whose_box_is_unticked_is_refused(self):
		with refused(), self.assertRaises(frappe.ValidationError):
			PathwayUpdate.validate(update(ingresses=0, servers=[row("Ingress Server", "ing1")]))

	def test_a_server_waits_at_most_once(self):
		with refused(), self.assertRaises(frappe.ValidationError):
			PathwayUpdate.validate(update(servers=[row("Gateway Server", "gw1"), row("Gateway Server", "gw1")]))

	def test_a_failed_server_can_be_added_again_to_retry_it(self):
		PathwayUpdate.validate(update(servers=[row("Gateway Server", "gw1", Status.Failure), row("Gateway Server", "gw1")]))

	def test_nothing_is_checked_while_it_runs_or_stops(self):
		for fields in ({"status": Status.Running}, {"status": Status.Pending, "started_at": "then"}):
			PathwayUpdate.validate(update(ingresses=0, servers=[row("Ingress Server", "ing1")], **fields))


class TestStartAndContinue(unittest.TestCase):
	def run_it(self, method, doc, running=None, statuses=None, published=200):
		statuses = statuses or {}
		db = SimpleNamespace(
			get_value=lambda doctype, name, field=None: running if doctype == "Pathway Update" else statuses.get(name, "Active"),
		)
		with (
			patch.object(frappe, "db", db),
			patch.object(module.requests, "head", return_value=SimpleNamespace(status_code=published)) as head,
		):
			method(doc)
		return head

	def ready(self, **fields):
		return update(servers=[row("Ingress Server", "ing1"), row("Gateway Server", "gw1")], **fields)

	def assert_refused(self, method, doc, **kwargs):
		with refused(), self.assertRaises(frappe.ValidationError):
			self.run_it(method, doc, **kwargs)
		doc.enqueue_steps.assert_not_called()

	def test_start_runs_a_pending_update(self):
		doc = self.ready()
		head = self.run_it(PathwayUpdate.start, doc)
		head.assert_called_once_with(
			"https://github.com/someone/pathway/releases/download/v0.0.4/sha256sums.txt", allow_redirects=True, timeout=10
		)
		self.assertEqual(Status.Running, doc.status)
		self.assertIsNotNone(doc.started_at)
		doc.enqueue_steps.assert_called_once()

	def test_start_is_only_for_a_pending_update(self):
		self.assert_refused(PathwayUpdate.start, self.ready(status="Stopped"))

	def test_start_refuses_an_update_still_stopping(self):
		# Pending again after Stop, its last server still deploying: a second job chain would race it.
		self.assert_refused(PathwayUpdate.start, self.ready(started_at="then"))

	def test_continue_carries_on_and_keeps_when_it_first_started(self):
		for status in ("Stopped", Status.Failure, Status.Success):
			doc = self.ready(status=status, started_at="then", ended_at="later", error="Traceback")
			with self.subTest(status):
				self.run_it(PathwayUpdate.resume, doc)
				self.assertEqual((Status.Running, "then", None, None), (doc.status, doc.started_at, doc.ended_at, doc.error))
				doc.enqueue_steps.assert_called_once()

	def test_continue_is_not_for_a_pending_or_running_update(self):
		for status in (Status.Pending, Status.Running):
			with self.subTest(status):
				self.assert_refused(PathwayUpdate.resume, self.ready(status=status))

	def test_nothing_pending_is_refused(self):
		self.assert_refused(PathwayUpdate.start, update(servers=[row("Gateway Server", "gw1", Status.Failure)]))

	def test_one_update_runs_at_a_time(self):
		self.assert_refused(PathwayUpdate.start, self.ready(), running="u0")

	def test_a_grove_settings_pin_that_moved_is_refused(self):
		self.assert_refused(PathwayUpdate.start, self.ready(release="v0.0.3"))

	def test_a_pending_server_no_longer_active_is_refused(self):
		self.assert_refused(PathwayUpdate.start, self.ready(), statuses={"gw1": "Broken"})

	def test_an_unpublished_release_is_refused(self):
		self.assert_refused(PathwayUpdate.start, self.ready(), published=404)

	def test_an_unreachable_github_raises(self):
		with (
			self.assertRaises(requests.ConnectionError),
			patch.object(module.requests, "head", side_effect=requests.ConnectionError("down")),
		):
			PathwayUpdate.check_release_is_published(self.ready())


class TestStop(unittest.TestCase):
	def test_stop_puts_it_back_to_pending_for_the_running_job_to_see(self):
		db = SimpleNamespace(set_value=Mock())
		with patch.object(frappe, "db", db):
			PathwayUpdate.stop(update(status=Status.Running))
		db.set_value.assert_called_once_with("Pathway Update", "u1", "status", Status.Pending, update_modified=False)

	def test_only_a_running_update_stops(self):
		with refused(), self.assertRaises(frappe.ValidationError):
			PathwayUpdate.stop(update(status="Stopped"))

	def execute(self, doc):
		with (
			patch.object(StepHandler, "_execute_steps") as runner,
			patch.object(frappe, "db", SimpleNamespace(commit=Mock())),
		):
			PathwayUpdate._execute_steps(doc, doc.servers, commit=True)
		return runner

	def test_the_next_server_does_not_start_after_a_stop(self):
		doc = update(status=Status.Pending, started_at="then", servers=[row("Gateway Server", "gw1", Status.Success), row("Gateway Server", "gw2")])
		self.execute(doc).assert_not_called()
		self.assertEqual("Stopped", doc.status)
		self.assertIsNotNone(doc.ended_at)

	def test_a_stop_after_the_last_server_still_finishes_the_update(self):
		doc = update(status=Status.Pending, started_at="then", servers=[row("Gateway Server", "gw1", Status.Success)])
		self.execute(doc).assert_called_once()


class TestDeployingAServer(unittest.TestCase):
	def deploy(self, deploy_agent, raises=None, **fields):
		"""The row after deploy_server ran against a server whose _deploy_agent is `deploy_agent`."""
		step = row("Gateway Server", "gw1")
		with (
			patch.object(frappe, "get_doc", return_value=SimpleNamespace(_deploy_agent=deploy_agent)),
			patch.object(frappe, "db", SimpleNamespace(commit=Mock())),
			refused(),
		):
			if raises:
				with self.assertRaises(raises):
					PathwayUpdate.deploy_server(update(**fields), step)
			else:
				PathwayUpdate.deploy_server(update(**fields), step)
		return step

	def test_a_deploy_that_worked_names_its_play_and_times(self):
		deploy_agent = Mock(return_value=("play-1", 0))
		step = self.deploy(deploy_agent)
		deploy_agent.assert_called_once_with(reference_doctype="Pathway Update", reference_docname="u1")
		self.assertEqual((Status.Success, "Ansible Play", "play-1"), (step.status, step.job_type, step.job))
		self.assertLessEqual(step.started_at, step.ended_at)

	def test_a_failed_play_marks_the_row_and_stops_the_update(self):
		step = self.deploy(Mock(return_value=("play-1", 1)), raises=frappe.ValidationError)
		self.assertEqual((Status.Failure, "play-1"), (step.status, step.job))
		self.assertIsNotNone(step.ended_at)

	def test_a_deploy_that_raises_still_records_when_it_ended(self):
		step = self.deploy(Mock(side_effect=RuntimeError("no Pathway Repo")), raises=RuntimeError)
		self.assertIsNotNone(step.ended_at)

	def test_a_pin_moved_mid_update_deploys_nothing(self):
		deploy_agent = Mock()
		step = self.deploy(deploy_agent, raises=frappe.ValidationError, release="v0.0.3")
		deploy_agent.assert_not_called()
		self.assertIsNotNone(step.ended_at)


class TestTheEnd(unittest.TestCase):
	def finish(self, method, *rows):
		doc = update(status=Status.Running, servers=list(rows))
		with patch.object(frappe, "db", SimpleNamespace(commit=Mock())):
			method(doc)
		return doc

	def test_every_server_deployed_is_a_success(self):
		doc = self.finish(PathwayUpdate.succeed, row("Gateway Server", "gw1", Status.Success))
		self.assertEqual(Status.Success, doc.status)
		self.assertIsNotNone(doc.ended_at)

	def test_a_server_passed_over_after_failing_fails_the_update(self):
		doc = self.finish(
			PathwayUpdate.succeed, row("Gateway Server", "gw1", Status.Failure), row("Gateway Server", "gw2", Status.Success)
		)
		self.assertEqual(Status.Failure, doc.status)

	def test_a_failure_records_when_it_ended(self):
		doc = self.finish(PathwayUpdate.fail)
		self.assertEqual(Status.Failure, doc.status)
		self.assertIsNotNone(doc.ended_at)

	def handle(self, already_reported=False, failed_type="Gateway Server", in_flight_answers=True):
		doc = update(status=Status.Failure, servers=[
			row("Gateway Server", "gw1", Status.Failure, output="earlier failure"),
			row(failed_type, "box2", Status.Failure, output="deploy_agent.yml failed"),
		])
		order = []
		server = SimpleNamespace(set_maintenance=Mock(side_effect=lambda on: order.append("maintenance")))
		if not in_flight_answers:
			server.set_maintenance.side_effect = requests.HTTPError("404")
		with (
			patch.object(frappe, "get_traceback", return_value="Traceback"),
			patch.object(frappe, "log_error") as log_error,
			patch.object(module.failure, "report") as report,
			patch.object(frappe, "local", SimpleNamespace(grove_failure_reported=already_reported)),
			patch.object(frappe, "db", SimpleNamespace(commit=lambda: order.append("commit"))),
			patch.object(frappe, "get_doc", return_value=server) as get_doc,
		):
			PathwayUpdate.handle_step_failure(doc)
		return doc, log_error, report, get_doc, server, order

	def test_a_failure_is_on_the_doc_and_in_the_error_log(self):
		doc, log_error, report, *_ = self.handle()
		self.assertEqual("Traceback", doc.error)
		log_error.assert_called_once_with(
			title="Pathway Update u1 failed", message="Traceback", reference_doctype="Pathway Update", reference_name="u1"
		)
		report.assert_called_once_with("Pathway Update", "u1", "Pathway update failed", "deploy_agent.yml failed")

	def test_the_failed_server_goes_into_maintenance_after_the_failure_is_committed(self):
		_, _, _, get_doc, server, order = self.handle()
		get_doc.assert_called_once_with("Gateway Server", "box2")
		server.set_maintenance.assert_called_once_with(1)
		self.assertEqual(["commit", "maintenance"], order)

	def test_a_failed_ingress_goes_into_maintenance_too(self):
		_, _, _, get_doc, server, _ = self.handle(failed_type="Ingress Server")
		get_doc.assert_called_once_with("Ingress Server", "box2")
		server.set_maintenance.assert_called_once_with(1)

	def test_a_server_that_cannot_take_maintenance_raises_after_the_record_is_kept(self):
		with self.assertRaises(requests.HTTPError):
			self.handle(in_flight_answers=False)

	def test_a_failed_play_that_already_announced_itself_is_not_announced_twice(self):
		_, log_error, report, *_ = self.handle(already_reported=True)
		log_error.assert_called_once()
		report.assert_not_called()
