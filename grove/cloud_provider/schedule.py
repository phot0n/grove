"""The daily window tick: a Pod names the time of day it should be up from and down from, and
this keeps it that way. Inside the window a pod with no provider pod is spawned — once per day,
marked on `scheduled_for`, so a spawn that gave up or a Stop pressed by hand is not undone a
minute later; outside it, a live pod is terminated. Nothing is consumed: the desired state is a
function of the clock, and what happened is on the Pod Activity rows the jobs write."""

import datetime

import frappe

SPAWN_TIMEOUT = 2100  # the button's budget: poll + engine gate + the create retries
TERMINATE_TIMEOUT = 600


class Window:
	"""A daily time window, which may cross midnight."""

	def __init__(self, up_from, down_from):
		self.up_from, self.down_from = as_time(up_from), as_time(down_from)

	def contains(self, moment):
		"""Whether the clock time of `moment` falls inside the window."""
		now = moment.time()
		if self.up_from < self.down_from:
			return self.up_from <= now < self.down_from
		return now >= self.up_from or now < self.down_from

	def started_on(self, moment):
		"""The date this window opened, for a moment inside it — yesterday when it crossed
		midnight and the clock is past it."""
		if self.up_from > self.down_from and moment.time() < self.down_from:
			return moment.date() - datetime.timedelta(days=1)
		return moment.date()


def as_time(value):
	"""A Time field arrives as a timedelta off the database and as text off a form."""
	if isinstance(value, datetime.time):
		return value
	return (datetime.datetime.min + frappe.utils.get_timedelta(value)).time()


def run_due_pods():
	"""Scheduled entry point: every pod with a window, reconciled to the clock."""
	now = frappe.utils.now_datetime()
	pods = frappe.get_all(
		"Pod",
		filters={"provision_at": ("is", "set"), "terminate_at": ("is", "set")},
		fields=["name", "provision_at", "terminate_at", "pod_id", "status", "scheduled_for"],
	)
	for pod in pods:
		try:
			reconcile(pod, now)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Pod window tick failed: {pod.name}")


def reconcile(pod, now):
	"""Spawn once per window while inside it and unprovisioned; terminate while outside and live."""
	window = Window(pod.provision_at, pod.terminate_at)
	if window.contains(now):
		if pod.pod_id:
			return
		frappe.db.set_value("Pod", pod.name, "scheduled_for", window.started_on(now))
		frappe.db.commit()
		enqueue("spawn_pod_doc", pod.name, SPAWN_TIMEOUT)
	elif pod.pod_id and pod.status != "Terminated":
		enqueue("terminate_pod_doc", pod.name, TERMINATE_TIMEOUT)


def enqueue(method, name, timeout):
	"""One job of a kind per pod at a time: a tick that runs while the last is still queued or
	running adds nothing."""
	frappe.enqueue(
		f"grove.cloud_provider.provisioner.{method}",
		queue="long",
		timeout=timeout,
		job_id=f"pod-{method}-{name}",
		deduplicate=True,
		pod_name=name,
		trigger="Scheduled",
	)
