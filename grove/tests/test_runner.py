# Copyright (c) 2026, Grove and contributors
# See license.txt
"""StepHandler's grove-specific paths. Pure: frappe is patched, steps and hosts are fakes."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grove import runner
from grove.runner import Status, StepHandler


class FakeStep(SimpleNamespace):
	def save(self):
		pass

	def reload(self):
		return self


class FakeHost:
	name = "gpu-box-1"

	def __init__(self, rc):
		self.rc, self.calls = rc, []

	def run_playbook(self, playbook, **kwargs):
		self.calls.append((playbook, kwargs))
		return "PLAY-1", self.rc


class Handler(StepHandler):
	doctype, name, status, error = "Machine Setup", "setup-1", None, None

	def save(self):
		pass

	def reload(self):
		return self

	def explode(self, step):
		raise RuntimeError("boom")


def raise_message(message):
	raise RuntimeError(message)


def fake_frappe():
	fake = MagicMock()
	fake.throw.side_effect = raise_message
	return fake


class TestAnsibleStep(unittest.TestCase):
	def test_success_links_the_play_back_to_this_doc(self):
		step, host = FakeStep(), FakeHost(rc=0)
		with patch.object(runner, "frappe", fake_frappe()):
			Handler().handle_ansible_play(step, host, "setup.yml")
		self.assertEqual((step.status, step.job, step.job_type), (Status.Success, "PLAY-1", "Ansible Play"))
		self.assertEqual(host.calls[0][1], {"reference_doctype": "Machine Setup", "reference_docname": "setup-1"})

	def test_nonzero_rc_fails_the_step_and_raises(self):
		step = FakeStep()
		with patch.object(runner, "frappe", fake_frappe()), self.assertRaisesRegex(RuntimeError, "PLAY-1"):
			Handler().handle_ansible_play(step, FakeHost(rc=2), "setup.yml")
		self.assertEqual(step.status, Status.Failure)


class TestMachineStatusStep(unittest.TestCase):
	def poll(self, machine_status, expected="Active"):
		fake, step = fake_frappe(), FakeStep(attempt=None)
		fake.get_doc.return_value.sync.side_effect = runner.CloudClientError("throttled")
		fake.db.get_value.return_value = machine_status
		with patch.object(runner, "frappe", fake):
			Handler().handle_machine_status(step, "gpu-box-1", expected)
		return step

	def test_keeps_polling_through_a_provider_error(self):
		step = self.poll("Pending")
		self.assertEqual((step.status, step.attempt), (Status.Running, 1))

	def test_passes_on_expected_status(self):
		self.assertEqual(self.poll("Active").status, Status.Success)

	def test_terminated_box_fails_instead_of_polling_forever(self):
		with self.assertRaisesRegex(RuntimeError, "Terminated"):
			self.poll("Terminated")


class TestExecuteSteps(unittest.TestCase):
	def test_raising_step_is_marked_failed_with_its_error(self):
		handler, step = Handler(), FakeStep(status=Status.Pending, method_name="explode")
		with patch.object(runner, "frappe", fake_frappe()) as fake:
			handler._execute_steps([step])
		self.assertEqual((step.status, step.output, handler.status), (Status.Failure, "boom", Status.Failure))
		fake.enqueue_doc.assert_not_called()

	def test_no_steps_left_succeeds(self):
		handler = Handler()
		with patch.object(runner, "frappe", fake_frappe()):
			handler._execute_steps([FakeStep(status=Status.Success)])
		self.assertEqual(handler.status, Status.Success)
