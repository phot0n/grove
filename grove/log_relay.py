# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Relay a live log stream onto a document's form over realtime, for as long as someone is
watching it.

Both log followers share this: a Pod reads its provider's SSE stream, a Model Replica reads
`docker logs --follow` over SSH. They differ only in where the lines come from, so each passes
an iterator of lines — with `None` for a tick (a keep-alive, or a spell of silence) so a quiet
stream still reaches the liveness check on time.

A stream runs while a viewer keeps saying so: the form pings keep_streaming, which re-sets one
redis key with a short TTL, and the relay stops as soon as that key is gone. Stop deletes it;
closing the tab, navigating away or a slept laptop simply stop refreshing it. The job holds a
background worker for its whole life, and a site has only a few — so a stream nobody is
watching has to end on its own rather than wait out the job timeout."""

import time

import frappe

ALIVE_KEY = "log_stream_alive:{}:{}"
# Long enough to ride out a slow ping, short enough that an abandoned stream frees its worker.
ALIVE_TTL = 45
FLUSH_INTERVAL = 0.25
EVENT = "grove_log"


def alive_key(doctype, docname):
	return ALIVE_KEY.format(frappe.scrub(doctype), docname)


def keep_alive(doctype, docname):
	"""Mark a stream as still watched. Called to start one, then again on every ping."""
	frappe.cache.set_value(alive_key(doctype, docname), 1, expires_in_sec=ALIVE_TTL)


def end(doctype, docname):
	"""Stop a running stream — the relay notices the key is gone at its next check."""
	frappe.cache.delete_value(alive_key(doctype, docname))


def is_alive(doctype, docname):
	"""use_local_cache=False: frappe.cache memoises reads in frappe.local, so a long-running
	job would keep re-reading its own first answer and never see the key expire."""
	return bool(frappe.cache.get_value(alive_key(doctype, docname), use_local_cache=False))


@frappe.whitelist()
def keep_streaming(doctype: str, docname: str):
	"""The viewer's ping. Permission-checked: this is reachable for any doctype, and refreshing
	a stream is only for someone allowed to read the document it belongs to."""
	frappe.has_permission(doctype, doc=docname, throw=True)
	keep_alive(doctype, docname)


def relay(events, doctype, docname):
	"""Publish `events` to the doc's form while a viewer is watching, or until the iterator
	ends. Returns when done; abandoning the iterator here is what shuts the source down."""
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
		if not is_alive(doctype, docname):
			break
	publish({"lines": lines, "done": True})
	end(doctype, docname)
