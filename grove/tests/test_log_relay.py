# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The shared log relay: batching, ticks, and the stop flag. Pure — realtime and the cache are
stubbed, so no site or redis."""

import unittest
from unittest.mock import patch

import frappe

from grove import log_relay


class FakeCache:
	"""Stands in for frappe.cache. Records nothing but the stop flag."""

	def __init__(self, stopped=False):
		self.values = {"stopped": stopped}

	def get_value(self, key, **kwargs):
		return 1 if self.values["stopped"] else None

	def set_value(self, key, val, **kwargs):
		self.values["stopped"] = True

	def delete_value(self, key, **kwargs):
		self.values["stopped"] = False


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

	def test_the_stop_flag_ends_the_stream_without_draining_it(self):
		# The generator must be abandoned, not exhausted — that is what kills `docker logs -f`.
		consumed = []

		def endless():
			while True:
				consumed.append(1)
				yield "line"

		published = run_relay(endless(), cache=FakeCache(stopped=True))
		self.assertTrue(published[-1]["done"])
		self.assertEqual(len(consumed), 1)

	def test_stopping_clears_the_flag_so_the_next_start_is_not_blocked(self):
		cache = FakeCache(stopped=True)
		run_relay(["a"], cache=cache)
		self.assertFalse(cache.values["stopped"])


if __name__ == "__main__":
	unittest.main()
