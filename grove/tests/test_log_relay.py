# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The shared log relay: batching, ticks, and the liveness key a viewer keeps refreshing.
Pure — realtime and the cache are stubbed, so no site or redis."""

import unittest
from unittest.mock import patch

import frappe

from grove import log_relay


class FakeCache:
	"""Stands in for frappe.cache. Holds only the stream's liveness key, which the real one
	expires on a TTL — here it is set or not."""

	def __init__(self, alive=True):
		self.values = {"alive": alive}

	def get_value(self, key, **kwargs):
		return 1 if self.values["alive"] else None

	def set_value(self, key, val, **kwargs):
		self.values["alive"] = True

	def delete_value(self, key, **kwargs):
		self.values["alive"] = False


def run_relay(events, cache=None):
	"""Relay `events` with realtime + cache stubbed; returns the published payloads."""
	published = []
	cache = cache or FakeCache()
	with (
		patch.object(frappe, "publish_realtime", lambda *a, **k: published.append(k["message"])),
		patch.object(frappe, "cache", cache),
		patch.object(log_relay, "FLUSH_INTERVAL", 0),  # publish per event, so batching is visible
	):
		log_relay.relay(iter(events), "Pod", "POD-1")
	return published


class TestRelay(unittest.TestCase):
	def test_lines_reach_the_form_and_the_stream_ends_with_done(self):
		published = run_relay(["a", "b"])
		self.assertEqual([p["lines"] for p in published], [["a"], ["b"], []])
		self.assertTrue(published[-1]["done"])

	def test_ticks_carry_no_line(self):
		# A keep-alive is a chance to flush and stop, not a log line — it must not print.
		published = run_relay([None, "a", None])
		self.assertEqual([line for p in published for line in p["lines"]], ["a"])

	def test_a_burst_batches_behind_the_first_line(self):
		published = []
		with (
			patch.object(frappe, "publish_realtime", lambda *a, **k: published.append(k["message"])),
			patch.object(frappe, "cache", FakeCache()),
		):
			log_relay.relay(iter(["a", "b", "c"]), "Pod", "POD-1")
		# The first line goes out at once (instant feedback); the rest wait out the 0.25s
		# interval and land together, rather than one socketio publish each.
		self.assertEqual(published, [{"lines": ["a"]}, {"lines": ["b", "c"], "done": True}])

	def test_a_stream_nobody_watches_ends_without_draining_the_source(self):
		# The viewer stopped refreshing the key (Stop, or they navigated away and it expired).
		# The generator must be abandoned, not exhausted — that is what kills `docker logs -f`
		# and frees the background worker the job is holding.
		consumed = []

		def endless():
			while True:
				consumed.append(1)
				yield "line"

		published = run_relay(endless(), cache=FakeCache(alive=False))
		self.assertTrue(published[-1]["done"])
		self.assertEqual(len(consumed), 1)

	def test_the_key_is_released_when_the_stream_ends(self):
		# Left behind, it would keep the next job alive with nobody watching it.
		cache = FakeCache(alive=True)
		run_relay(["a"], cache=cache)
		self.assertFalse(cache.values["alive"])


if __name__ == "__main__":
	unittest.main()
