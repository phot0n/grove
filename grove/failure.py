"""Announce a background failure instead of leaving it in the RQ job.

Grove's work happens in workers: a button enqueues, the caller gets "queued, watch its Ansible
Plays", and the browser moves on. Until this existed, a failure after that point was invisible —
no toast, no notification, and often nothing on the doc either. The whole record was the job's
traceback, which you have to know to go looking for.

Three things go wrong, and they surface differently:

- **The job raises.** Nothing catches it, so the doc keeps whatever transitional status it was set
  to on the way in. `reports_failure` covers this.
- **A playbook returns non-zero.** `ansible_runner.run_play` returns `(name, 1)` and never raises,
  so a failed play is only as visible as its caller chose to make it — and six callers discard the
  code entirely. `AnsiblePlay.on_update` covers this, for every caller at once.
- **A provider call dies mid-lifecycle.** `PodProvisioner.fail` deliberately swallows and returns;
  it calls `report` directly.

What it does NOT do is decide anything. Reporting is not error handling: `reports_failure` re-raises
so the worker still fails the job and Frappe still writes its Error Log.
"""

import functools

import frappe
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

# Statuses a doc is only ever in while a job is working on it. A failure may overwrite one of these
# and nothing else — a box that was torn down mid-play is Terminated, not Broken, and one that was
# already Active before a config push failed has not stopped being Active.
IN_PROGRESS = ("Installing", "Provisioning")


def report(doctype, name, title, detail, *, mark_broken=False):
	"""Put a failure where someone will see it.

	Three audiences, because they miss each other: the toast reaches whoever is still on the page,
	the notification reaches them after they have closed it, and the comment is what is still there
	next week when someone asks what happened to this box.
	"""
	if not doctype or not name:
		# A play with no reference doc — nothing to comment on and nothing to link a notification
		# to. The job's Error Log is the record for that one.
		return
	frappe.local.grove_failure_reported = True

	if mark_broken:
		_mark_broken(doctype, name)

	_toast(doctype, name, title, detail)
	_notify(doctype, name, title, detail)


def reports_failure(mark_broken=False, doctype=None):
	"""Decorator for something that runs in a worker. Reports, then re-raises.

	Re-raising is the point: this is a reporting layer, not a rescue. The worker still marks the job
	failed and Frappe still writes the Error Log with its traceback — this only adds the half a
	human sees.

	Wraps both shapes Grove enqueues. A bound method finds the doc on `self`; a module-level job
	function takes the docname as its first argument, and names its doctype here.
	"""

	def decorate(method):
		@functools.wraps(method)
		def wrapper(*args, **kwargs):
			try:
				return method(*args, **kwargs)
			except Exception as error:
				# A failed play already reported through AnsiblePlay.on_update. If the caller then
				# raised on the same failure, this is the second half of one event, not a second
				# event — same worker, same job, so frappe.local still remembers.
				if not getattr(frappe.local, "grove_failure_reported", False):
					failed = args[0] if args else None
					report(
						doctype or getattr(failed, "doctype", None),
						failed if doctype else getattr(failed, "name", None),
						f"{_label(method)} failed",
						str(error),
						mark_broken=mark_broken,
					)
				raise

		return wrapper

	return decorate


def _mark_broken(doctype, name):
	"""Stop the doc claiming it is still mid-install.

	The status that was set on the way in is what made the original failure hard to find: a Gateway
	Server sat at Installing indefinitely, looking like a slow provision rather than a dead one.

	Only from a transitional status, and only when the doctype has somewhere to go. Written with
	db.set_value to match every other status write on these paths — those are deliberately outside
	the document lifecycle so a provision does not re-trigger a sync.
	"""
	meta = frappe.get_meta(doctype)
	status_field = meta.get_field("status")
	if not status_field or "Broken" not in (status_field.options or "").split("\n"):
		return
	if frappe.db.get_value(doctype, name, "status") not in IN_PROGRESS:
		return
	frappe.db.set_value(doctype, name, "status", "Broken")
	frappe.db.commit()


def _toast(doctype, name, title, detail):
	"""The half that arrives while someone is still looking.

	execute_job sets the session to whoever enqueued the job, so this lands on the person who
	clicked the button with no room or subscription to arrange. Fire-and-forget by nature — if they
	navigated away it is gone, which is what the notification below is for.
	"""
	try:
		frappe.msgprint(
			f"{name}: {detail}" if detail else name,
			title=title,
			indicator="red",
			alert=True,
			realtime=True,
		)
	except Exception:
		frappe.log_error(title=f"Could not toast a failure on {doctype} {name}")


def _notify(doctype, name, title, detail):
	"""The half that survives a closed tab.

	type="Alert" is load-bearing: make_notification_logs drops a notification whose recipient is
	also its sender for every other type, and someone provisioning their own box is exactly that.

	Administrator is skipped by the framework, so a job with no real session — the scheduler —
	produces no notification here. That is a different audience needing a different channel, and it
	is why the scheduled syncs are not wired into this.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		return

	try:
		enqueue_create_notification(
			user,
			{
				"type": "Alert",
				"document_type": doctype,
				"document_name": name,
				"subject": f"{title}: <b class='subject-title'>{name}</b>",
				"email_content": detail,
				"from_user": user,
			},
		)
	except Exception:
		frappe.log_error(title=f"Could not notify a failure on {doctype} {name}")


def _label(method):
	"""'_deploy_agent' → 'Deploy agent'. What the operator pressed, near enough."""
	return method.__name__.lstrip("_").replace("_", " ").capitalize()
