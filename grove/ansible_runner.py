# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Run Ansible playbooks against a Machine from a background job, streaming
per-task events into Ansible Play / Ansible Task docs.

Modelled on Frappe Press's `press/runner.py`: drive Ansible in-process via
`PlaybookExecutor` with a custom `CallbackBase`, rather than shelling out. The
callback creates an Ansible Task per task and updates its status live (the
Ansible Task `on_update` publishes realtime to the parent play)."""

import json
import os

from ansible import constants as C
from ansible import context
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.inventory.manager import InventoryManager
from ansible.module_utils.common.collections import ImmutableDict
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.callback import CallbackBase
from ansible.plugins.loader import init_plugin_loader
from ansible.vars.manager import VariableManager

import frappe


def _set_global_cli_args(remote_user, tags=None, skip_tags=None):
	# PlaybookExecutor/TaskQueueManager read a broad set of CLI args from this
	# process-global. Set a complete-enough ImmutableDict to avoid KeyErrors.
	# tags=["all"] runs every task; pass a subset (e.g. ["unit"]) to run only the
	# matching tasks — used for a fast unit-only reconfigure.
	context.CLIARGS = ImmutableDict(
		connection="smart",
		module_path=[],
		forks=1,
		remote_user=remote_user,
		private_key_file=None,
		# Keepalive so long silent tasks (an engine image pull moves tens of GB with no
		# channel output) don't get dropped by a NAT/idle timeout — which used to kill
		# the child on the box. 30s pings, tolerate ~1h before giving up.
		ssh_common_args=(
			"-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "
			"-o ServerAliveInterval=30 -o ServerAliveCountMax=120 -o TCPKeepAlive=yes"
		),
		ssh_extra_args=None,
		sftp_extra_args=None,
		scp_extra_args=None,
		become=True,
		become_method="sudo",
		become_user="root",
		become_ask_pass=False,
		ask_pass=False,
		verbosity=0,
		check=False,
		diff=False,
		syntax=None,
		start_at_task=None,
		listhosts=None,
		listtasks=None,
		listtags=None,
		tags=tags or ["all"],
		skip_tags=skip_tags or [],
		step=None,
		timeout=30,
	)
	C.HOST_KEY_CHECKING = False
	# Required (ansible-core >= 2.15) before the in-process API can resolve
	# collections/modules like ansible.builtin.*.
	init_plugin_loader()


class AnsibleCallback(CallbackBase):
	"""Maps Ansible v2 callbacks → Ansible Task / Ansible Play doc updates."""

	def __init__(self, runner):
		super().__init__()
		self.runner = runner
		self.current_task = None  # current Ansible Task doc name
		self.stopped = False
		self.site = frappe.local.site
		self.thread_db = None  # the results thread's own connection, if it needed one
		self.log = []

	# --- play lifecycle ---
	def v2_playbook_on_start(self, playbook):
		self.runner.update_play({"status": "Running", "started": frappe.utils.now()})

	def v2_playbook_on_task_start(self, task, is_conditional):
		self._stop_if_asked()
		self.current_task = self.runner.add_task(task.get_name())

	# Fires on every attempt of a task with `until`/`retries` — the health gate is one task
	# that can hold the play for 15 minutes, so this is where a stop lands for most of a
	# serve run. Between tasks alone would not be enough.
	def v2_runner_retry(self, result):
		self._stop_if_asked()

	def _stop_if_asked(self):
		"""The Stop button writes Stopping from the web process; this is the worker seeing
		it. Commit first — without ending the current transaction the read is a snapshot
		from before the button. Ansible swallows callback exceptions, so raising here would
		be silently ignored; terminate() is the supported way out and the linear strategy
		checks it both between tasks and while waiting on the running one."""
		if self.stopped:
			return
		self._bind_frappe()
		frappe.db.commit()
		if frappe.db.get_value("Ansible Play", self.runner.play_name, "status") == "Stopping":
			self.stopped = True
			self.runner.terminate()

	def _bind_frappe(self):
		"""Ansible hands retry callbacks to its results thread, and frappe.local is a
		contextvar — a new thread starts with none of it, so frappe.db there raises
		"object is not bound". Give that thread a connection of its own rather than
		reaching across to the main thread's; the runner closes it when the play ends."""
		try:
			frappe.db.get_value  # noqa: B018 — unbound frappe.local raises on attribute access
			return
		except RuntimeError:
			frappe.init(site=self.site)
			frappe.connect()
			self.thread_db = frappe.db

	def v2_playbook_on_stats(self, stats):
		hosts = sorted(stats.processed.keys())
		failed = any(
			stats.summarize(h).get("failures", 0) or stats.summarize(h).get("unreachable", 0)
			for h in hosts
		)
		# A stopped play ran its tasks to whatever point it reached, so the stats can read
		# clean; the operator's intent is what the status has to show.
		self.runner.update_play({
			"status": "Stopped" if self.stopped else ("Failure" if failed else "Success"),
			"ended": frappe.utils.now(),
			"output": "\n".join(self.log)[:1000000],
		})

	# --- task results ---
	def v2_runner_on_ok(self, result):
		changed = bool(result._result.get("changed"))
		self._finish_task(result, "changed" if changed else "ok")

	def v2_runner_on_failed(self, result, ignore_errors=False):
		self._finish_task(result, "failed")

	def v2_runner_on_skipped(self, result):
		self._finish_task(result, "skipped")

	def v2_runner_on_unreachable(self, result):
		self._finish_task(result, "unreachable")

	def _finish_task(self, result, status):
		name = getattr(result._task, "name", "") or ""
		self.log.append(f"[{status}] {name}")
		if not self.current_task:
			return
		self.runner.finish_task(self.current_task, status, result._result)
		self.current_task = None


class Ansible:
	"""Runs one playbook against one host, logging to Ansible Play/Task."""

	def __init__(self, playbook_path, host, server_type, server, user="root", port=22, variables=None, tags=None, skip_tags=None, reference_doctype=None, reference_docname=None):
		self.playbook_path = playbook_path
		self.playbook = os.path.basename(playbook_path)
		self.host = host
		self.user = user or "root"
		self.port = port or 22
		self.variables = variables or {}
		self.server_type = server_type
		self.server = server
		self.reference_doctype = reference_doctype
		self.reference_docname = reference_docname

		_set_global_cli_args(self.user, tags=tags, skip_tags=skip_tags)
		self.loader = DataLoader()
		self.inventory = InventoryManager(loader=self.loader, sources=f"{self.host},")
		h = self.inventory.get_host(self.host)
		h.set_variable("ansible_port", self.port)
		h.set_variable("ansible_user", self.user)
		h.set_variable("ansible_ssh_common_args", "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20")
		self.variable_manager = VariableManager(loader=self.loader, inventory=self.inventory)
		self.variable_manager._extra_vars = self.variables

		self.play_name = self._create_play()
		self.callback = AnsibleCallback(self)
		self.executor = None

	def _create_play(self):
		doc = frappe.get_doc({
			"doctype": "Ansible Play",
			"server_type": self.server_type,
			"server": self.server,
			"reference_doctype": self.reference_doctype,
			"reference_docname": self.reference_docname,
			"playbook": self.playbook,
			"status": "Pending",
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	# --- called from the callback ---
	def update_play(self, values):
		doc = frappe.get_doc("Ansible Play", self.play_name)
		if values.get("ended") and doc.started:
			values["duration"] = frappe.utils.time_diff_in_seconds(values["ended"], doc.started)
		doc.update(values)
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	def add_task(self, task_name):
		doc = frappe.get_doc({
			"doctype": "Ansible Task",
			"play": self.play_name,
			"task_name": task_name,
			"host": self.host,
			"status": "ok",
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def finish_task(self, task_doc_name, status, result):
		doc = frappe.get_doc("Ansible Task", task_doc_name)
		doc.status = status
		try:
			doc.output = json.dumps(result, default=str, indent=2)[:100000]
		except Exception:
			doc.output = str(result)[:100000]
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	def terminate(self):
		"""Stop the run where it is. Ansible's own SIGINT path does exactly this: the
		strategy checks the flag between tasks AND while waiting on the running one, then
		unwinds through PlaybookExecutor's cleanup, which reaps the worker."""
		if self.executor:
			self.executor._tqm.terminate()

	def run(self):
		self.executor = PlaybookExecutor(
			playbooks=[self.playbook_path],
			inventory=self.inventory,
			variable_manager=self.variable_manager,
			loader=self.loader,
			passwords={},
		)
		self.executor._tqm._stdout_callback = self.callback
		try:
			self.executor.run()
		finally:
			if self.callback.thread_db:
				self.callback.thread_db.close()
			frappe.db.commit()
		return frappe.get_doc("Ansible Play", self.play_name)


def run_play(playbook, server_type, server_name, machine_name, project_dir, extravars=None, tags=None, skip_tags=None, reference_doctype=None, reference_docname=None):
	"""Build a single-host inventory from the Machine and run
	<project_dir>/<playbook>. Returns (ansible_play_name, rc); rc 0 = success.
	tags restricts the run to matching tasks; skip_tags excludes them (e.g.
	skip_tags=["heavy"] for a fast reconfigure that skips the weights predownload).
	reference_doctype/docname link the Ansible Play to the triggering doc (defaults to server_type/server_name)."""
	m = frappe.get_doc("Machine", machine_name)
	if not m.public_ip:
		frappe.throw(f"Machine {machine_name} has no public_ip")

	# Cloud GPU images (RunPod) ship several pythons; Ansible's interpreter auto-discovery
	# can land on one lacking apt/cffi bindings → the apt module crashes. Pin the distro
	# python (has python3-apt + cffi). Extra-var = highest precedence; explicit wins.
	extravars = dict(extravars or {})
	if m.cloud_provider:
		extravars.setdefault("ansible_python_interpreter", "/usr/bin/python3")

	ansible = Ansible(
		playbook_path=os.path.join(project_dir, playbook),
		host=m.public_ip,
		server_type=server_type,
		server=server_name,
		user=m.ssh_user or "root",
		port=m.ssh_port or 22,
		variables=extravars,
		tags=tags,
		skip_tags=skip_tags,
		reference_doctype=reference_doctype,
		reference_docname=reference_docname,
	)
	play = ansible.run()
	return play.name, (0 if play.status == "Success" else 1)
