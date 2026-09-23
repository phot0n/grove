# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
import requests
from frappe.model.document import Document
from frappe.utils import now_datetime

from grove import failure
from grove.fleet import gateway_agent_version, pathway_repo
from grove.runner import Status, StepHandler

# Order rows are added at insert. Ingresses first: they sit downstream of the gateways that call them.
SERVER_TYPES = (("ingresses", "Ingress Server"), ("gateways", "Gateway Server"))
STOPPED = "Stopped"


class PathwayUpdate(StepHandler, Document):
	"""Installs Grove Settings' pathway release on the chosen gateways and ingresses, one server at a
	time. Started by hand; a failed deploy stops it, Continue passes over the failed row, and Stop
	takes effect between servers."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.pathway_update_server.pathway_update_server import PathwayUpdateServer

		ended_at: DF.Datetime | None
		error: DF.Code | None
		gateways: DF.Check
		ingresses: DF.Check
		release: DF.Data | None
		repo: DF.Data | None
		servers: DF.Table[PathwayUpdateServer]
		started_at: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Stopped", "Success", "Failure"]
	# end: auto-generated types

	def before_insert(self):
		"""The release Grove Settings pins now, and every Active server of each ticked type."""
		if not (self.ingresses or self.gateways):
			frappe.throw("Tick Gateways or Ingresses — the update needs servers to deploy to.")
		self.release, self.repo = gateway_agent_version(), pathway_repo()
		self.set("servers", [])
		for checkbox, doctype in SERVER_TYPES:
			if self.get(checkbox):
				for server in frappe.get_all(doctype, filters={"status": "Active"}, order_by="name asc", pluck="name"):
					self.append("servers", {"server_type": doctype, "server": server})

	def validate(self):
		"""Rows added by hand: only a ticked type, each server Pending at most once. Skipped while
		running, when the only saves are the runner's own."""
		if self.is_in_progress:
			return
		allowed = [doctype for checkbox, doctype in SERVER_TYPES if self.get(checkbox)]
		pending = set()
		for row in self.servers:
			row.status = row.status or Status.Pending
			if row.server_type not in allowed:
				frappe.throw(f"Row {row.idx}: this update deploys to {' and '.join(allowed)} only.")
			if row.status != Status.Pending:
				continue
			if row.server in pending:
				frappe.throw(f"Row {row.idx}: {row.server} is already waiting to be deployed.")
			pending.add(row.server)
			row.step_name, row.method_name = f"Deploy pathway to {row.server}", "deploy_server"

	@property
	def is_in_progress(self):
		"""Running, or back to Pending after a Stop while its current server finishes."""
		return self.status == Status.Running or (self.status == Status.Pending and bool(self.started_at))

	@frappe.whitelist()
	def start(self):
		if self.is_in_progress:
			frappe.throw(f"Pathway Update {self.name} is still deploying — wait until it is Stopped, then Continue.")
		if self.status != Status.Pending:
			frappe.throw(f"Pathway Update {self.name} is {self.status} — use Continue.")
		self.run(started_at=now_datetime())

	@frappe.whitelist()
	def resume(self):
		"""Button: carry on with the Pending rows. A failed row stays Failure and is passed over."""
		if self.status in (Status.Pending, Status.Running):
			frappe.throw(f"Pathway Update {self.name} is {self.status}.")
		self.run(error=None, ended_at=None)

	def run(self, **fields):
		self.check_can_run()
		self.update({"status": Status.Running, **fields})
		self.save()
		self.enqueue_steps()

	def check_can_run(self):
		pending = [row for row in self.servers if row.status == Status.Pending]
		if not pending:
			frappe.throw("No Pending servers — add a row for each server to update.")
		running = frappe.db.get_value(
			"Pathway Update",
			{"status": ("in", (Status.Running, Status.Pending)), "started_at": ("is", "set"), "name": ("!=", self.name)},
		)
		if running:
			frappe.throw(f"Pathway Update {running} is still running.")
		self.check_settings_unchanged()
		inactive = [row.server for row in pending if frappe.db.get_value(row.server_type, row.server, "status") != "Active"]
		if inactive:
			frappe.throw(f"Not Active: {', '.join(inactive)} — delete their rows first.")
		self.check_release_is_published()

	def check_settings_unchanged(self):
		"""A deploy installs whatever Grove Settings pins at that moment, so it must still be this update's."""
		pinned = (gateway_agent_version(), pathway_repo())
		if pinned != (self.release, self.repo):
			frappe.throw(
				f"Grove Settings now pins {pinned[0]} from {pinned[1]}, not {self.release} from {self.repo} "
				"— create a new Pathway Update."
			)

	def check_release_is_published(self):
		"""The asset every deploy checks its download against, so a typo fails before any box is touched."""
		url = f"https://github.com/{self.repo}/releases/download/{self.release}/sha256sums.txt"
		response = requests.head(url, allow_redirects=True, timeout=10)
		if response.status_code != 200:
			frappe.throw(f"{self.repo} has no published release {self.release}: {url} answered {response.status_code}.")

	@frappe.whitelist()
	def stop(self):
		"""Button: stop once the server being deployed is done, never in the middle of one. Pending
		until then; the next job sees it and marks the update Stopped."""
		if self.status != Status.Running:
			frappe.throw(f"Pathway Update {self.name} is {self.status}.")
		# Not modified: the running job saves this doc again, and a newer timestamp would fail that save.
		frappe.db.set_value(self.doctype, self.name, "status", Status.Pending, update_modified=False)

	def enqueue_steps(self):
		frappe.enqueue_doc(
			self.doctype, self.name, "_execute_steps", steps=self.servers, commit=True,
			queue="long", timeout=18000, enqueue_after_commit=True,
		)

	def _execute_steps(self, steps, **kwargs):
		"""Each server runs in its own job, so a Stop is honoured here, before the next one starts."""
		if self.status == Status.Pending and self.next_step(self.servers):
			self.status, self.ended_at = STOPPED, now_datetime()
			self.save()
			frappe.db.commit()
			return
		super()._execute_steps(steps, **kwargs)

	def deploy_server(self, step):
		"""Deploy pathway"""
		step.status, step.started_at = Status.Running, now_datetime()
		step.save()
		frappe.db.commit()
		try:
			self.check_settings_unchanged()
			server = frappe.get_doc(step.server_type, step.server)
			play_name, rc = server._deploy_agent(reference_doctype=self.doctype, reference_docname=self.name)
		finally:
			# On a raise, _execute_steps saves this same row as Failure.
			step.ended_at = now_datetime()
		step.job_type, step.job = "Ansible Play", play_name
		step.status = Status.Success if rc == 0 else Status.Failure
		step.save()
		if rc != 0:
			frappe.throw(f"deploy_agent.yml failed on {step.server} (Ansible Play {play_name}).")

	def succeed(self, success_status=Status.Success):
		"""No Pending row left. A row that failed earlier and was passed over still fails the update."""
		self.ended_at = now_datetime()
		if any(row.status == Status.Failure for row in self.servers):
			super().fail(Status.Failure)
		else:
			super().succeed(success_status)

	def fail(self, failure_status=Status.Failure):
		self.ended_at = now_datetime()
		super().fail(failure_status)

	def handle_step_failure(self):
		"""The traceback on the doc and in the Error Log, announced unless a failed play already was.
		The failed server goes into maintenance to be debugged."""
		super().handle_step_failure()
		frappe.log_error(
			title=f"Pathway Update {self.name} failed",
			message=self.error,
			reference_doctype=self.doctype,
			reference_name=self.name,
		)
		failed = [row for row in self.servers if row.status == Status.Failure][-1]
		if not getattr(frappe.local, "grove_failure_reported", False):
			failure.report(self.doctype, self.name, "Pathway update failed", failed.output)
		# Committed first: a server that cannot answer /grove-admin/in-flight raises, and the job's
		# rollback must not take the failure record with it.
		frappe.db.commit()
		frappe.get_doc(failed.server_type, failed.server).set_maintenance(1)
