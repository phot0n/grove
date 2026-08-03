# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Relay a live log stream onto a document's form over realtime, until the Stop button.

Both log followers share this: a Pod reads its provider's SSE stream, a Model Deployment reads
`docker logs --follow` over SSH. They differ only in where the lines come from, so each passes
an iterator of lines — with `None` for a tick (a keep-alive, or a spell of silence) so a quiet
stream still reaches the stop check on time."""

import time

import frappe

STOP_KEY = "log_stream_stop:{}:{}"
FLUSH_INTERVAL = 0.25
EVENT = "grove_log"


def stop_key(doctype, docname):
	return STOP_KEY.format(frappe.scrub(doctype), docname)


def request_stop(doctype, docname):
	"""Ask a running relay to finish. Expires, so a job that dies mid-stream cannot leave a
	flag behind that stops the next one before it starts."""
	frappe.cache.set_value(stop_key(doctype, docname), 1, expires_in_sec=3600)


def clear_stop(doctype, docname):
	frappe.cache.delete_value(stop_key(doctype, docname))


def is_stopped(doctype, docname):
	"""use_local_cache=False: frappe.cache memoises reads in frappe.local, so a long-running
	job would keep re-reading its own first (empty) answer and never see the Stop button."""
	return bool(frappe.cache.get_value(stop_key(doctype, docname), use_local_cache=False))


def relay(events, doctype, docname):
	"""Publish `events` to the doc's form until Stop, or until the iterator ends. Returns when
	done; abandoning the iterator here is what shuts the underlying stream down."""
	lines, flushed_at = [], 0.0

	def publish(payload):
		frappe.publish_realtime(EVENT, doctype=doctype, docname=docname, message=payload)

	for line in events:
		if line is not None:
			lines.append(line)
		# Batch: an engine spews thousands of lines while loading a model, and one publish
		# each floods socketio. A quiet stream still gets here on its ticks.
		if time.monotonic() - flushed_at < FLUSH_INTERVAL:
			continue
		if lines:
			publish({"lines": lines})
		lines, flushed_at = [], time.monotonic()
		if is_stopped(doctype, docname):
			break
	publish({"lines": lines, "done": True})
	clear_stop(doctype, docname)
