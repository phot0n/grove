# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The frozen-once-published rule. Pure — the two doc states are passed in, so no site
needed."""

import unittest
from types import SimpleNamespace

from grove.grove.doctype.model.model import is_scheduling_policy_frozen


def model(published=0, scheduling_policy="priority"):
	return SimpleNamespace(published=published, scheduling_policy=scheduling_policy)


class TestSchedulingPolicyFreeze(unittest.TestCase):
	def test_published_model_cannot_change_policy(self):
		self.assertTrue(is_scheduling_policy_frozen(model(published=1), model(1, "fcfs")))

	def test_unpublished_model_can_change_policy(self):
		self.assertFalse(is_scheduling_policy_frozen(model(), model(0, "fcfs")))

	def test_unchanged_policy_is_not_frozen(self):
		# Everything else on a published model stays editable.
		self.assertFalse(is_scheduling_policy_frozen(model(published=1), model(published=1)))

	def test_insert_has_nothing_to_freeze(self):
		self.assertFalse(is_scheduling_policy_frozen(None, model(1, "fcfs")))


if __name__ == "__main__":
	unittest.main()
