from enum import Enum
from typing import TYPE_CHECKING, Literal

import frappe
from frappe.model.document import Document

from grove.cloud_provider.base import CloudClientError

if TYPE_CHECKING:
	from grove.ansible import AnsibleHost


class Status(str, Enum):
	Pending = "Pending"
	Running = "Running"
	Success = "Success"
	Skipped = "Skipped"
	Failure = "Failure"

	def __str__(self):
		return self.value


class GenericStep(Document):
	attempt: int
	job_type: Literal["Ansible Play"]
	job: str | None
	status: Status
	method_name: str
	output: str | None


class StepHandler:
	"""Mixin for a Document that runs its steps one background job at a time."""

	doctype: str
	name: str

	def handle_machine_status(self, step: GenericStep, machine: str, expected_status: str) -> None:
		"""Poll step: sync the Machine off its cloud and pass once it reports `expected_status`."""
		step.attempt = (step.attempt or 0) + 1

		try:
			frappe.get_doc("Machine", machine).sync()
		except CloudClientError:
			pass  # transient provider error, the next attempt syncs again

		machine_status = frappe.db.get_value("Machine", machine, "status")
		if machine_status == "Terminated" and expected_status != "Terminated":
			frappe.throw(f"Machine {machine} is Terminated, it will never reach {expected_status}.")
		step.status = Status.Success if machine_status == expected_status else Status.Running
		step.save()

	def handle_ansible_play(self, step: GenericStep, host: "AnsibleHost", playbook: str, **kwargs) -> None:
		"""Run `playbook` on `host`'s box. The play references this doc, so its failure lands here."""
		play_name, rc = host.run_playbook(
			playbook, reference_doctype=self.doctype, reference_docname=self.name, **kwargs
		)
		step.job_type = "Ansible Play"
		step.job = play_name
		step.status = Status.Success if rc == 0 else Status.Failure
		step.save()

		if step.status == Status.Failure:
			frappe.throw(f"{playbook} failed on {host.name} (Ansible Play {play_name}).")

	def fail(self, failure_status: str = Status.Failure):
		self.status = failure_status
		self.save()
		frappe.db.commit()

	def succeed(self, success_status: str = Status.Success):
		self.status = success_status
		self.save()
		frappe.db.commit()

	def handle_step_failure(self):
		# can be overridden by controllers
		self.error = frappe.get_traceback(with_context=True)
		self.save()

	def get_steps(self, methods: list) -> list[dict]:
		"""Step rows for `methods`, named by each method's docstring."""
		return [
			{
				"step_name": method.__doc__,
				"method_name": method.__name__,
				"status": Status.Pending,
			}
			for method in methods
		]

	def _get_method(self, method_name: str, method_objects: list[object] | None = None):
		"""Retrieve a method object by name."""
		method_objects = method_objects or []
		for method_object in method_objects:
			if hasattr(method_object, method_name):
				return getattr(method_object, method_name)
		return getattr(self, method_name)

	def next_step(self, steps: list[GenericStep]) -> GenericStep | None:
		for step in steps:
			if step.status not in (Status.Success, Status.Failure, Status.Skipped):
				return step

		return None

	def _execute_steps(
		self,
		steps: list[GenericStep],
		commit: bool = False,
		start_status: str = Status.Running,
		success_status: str = Status.Success,
		failure_status: str = Status.Failure,
		method_objects: list[object] | None = None,
	):
		"""Run one step, then enqueue the next. Call it through `enqueue_doc`, or the first step runs
		in the web worker."""
		self.status = start_status
		self.save()

		step = self.next_step(steps)
		if not step:
			self.succeed(success_status)
			return

		step = step.reload()
		method = self._get_method(method_objects=method_objects, method_name=step.method_name)

		try:
			method(step)
		except Exception as error:
			step.status = Status.Failure
			step.output = str(error)
			step.save()
			self.reload()
			self.fail(failure_status)
			self.handle_step_failure()
			return

		if commit:
			frappe.db.commit()

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_execute_steps",
			method_objects=method_objects,
			steps=steps,
			commit=commit,
			start_status=start_status,
			success_status=success_status,
			failure_status=failure_status,
			timeout=18000,
			at_front=True,
			queue="long",
			enqueue_after_commit=True,
		)
