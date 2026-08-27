# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""A card is held by exactly one replica, and the database is what makes that true.

The claim's NAME is `<inference server>:<cuda index>`, so a second claim on the same card is a
primary-key collision rather than a check that might be racing something. These tests are about
the naming, which is the whole mechanism — that two concurrent inserts cannot both win is a
property of the PK, not of any code here.

The real two-connection concurrency test lives in the bench run described in the plan: a unit test
with one connection cannot observe an interleaving, and a concurrency test that has never been seen
to fail is not evidence."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.gpu_claim.gpu_claim import GPUClaim, claim_name
from grove.grove.doctype.model_replica.model_replica import (
	CLAIM_HOLDING_STATUSES,
	GPU_CLAIMING_STATUSES,
)


def named(machine, gpu_index):
	doc = SimpleNamespace(machine=machine, gpu_index=gpu_index, name=None)
	GPUClaim.autoname(doc)
	return doc.name


class TestTheNameIsTheClaim(unittest.TestCase):
	def test_a_card_names_itself_once(self):
		self.assertEqual(named("mc-blackwell", 0), "mc-blackwell:0")

	def test_two_servers_on_one_machine_compete_for_the_same_card(self):
		# A GPU is a `Machine GPU` row, and `Inference Server.machine` is neither unique nor
		# checked. Keyed on the server, two servers naming one machine could each claim card 0 of
		# the same physical box; keyed on the machine they collide, which is the point.
		inf_a, inf_b = SimpleNamespace(machine="mc-blackwell"), SimpleNamespace(machine="mc-blackwell")
		self.assertEqual(
			claim_name(inf_a.machine, 0),
			claim_name(inf_b.machine, 0),
		)

	def test_two_replicas_wanting_one_card_want_one_name(self):
		# The entire concurrency design in one assertion: whatever order two placements arrive
		# in, they are competing to insert the same primary key, and only one insert can win.
		self.assertEqual(named("mc-blackwell", 0), named("mc-blackwell", 0))

	def test_different_cards_on_one_box_are_different_names(self):
		self.assertNotEqual(named("mc-blackwell", 0), named("mc-blackwell", 1))

	def test_the_same_index_on_two_boxes_does_not_collide(self):
		# Every machine counts its cards from 0, so the machine has to be in the name or card 0
		# would be a single fleet-wide resource.
		self.assertNotEqual(named("mc-blackwell", 0), named("mc-aws-2", 0))

	def test_a_string_index_still_names_an_int(self):
		# A whitelisted call and a JS dialog both hand over strings. "0" and 0 must be one card,
		# or a claim could be taken twice under two spellings.
		self.assertEqual(named("mc-blackwell", "0"), named("mc-blackwell", 0))

	def test_the_separator_cannot_occur_in_a_server_name(self):
		# Machine names carry dashes (`mc-blackwell`), so a dash separator would make the split
		# ambiguous. A colon cannot appear in one.
		self.assertNotIn(":", "mc-blackwell")
		self.assertIn(":", named("mc-blackwell", 0))


class TestWhichStatusesHold(unittest.TestCase):
	def test_draft_holds_but_is_not_a_serving_status(self):
		# Draft holds its cards from insert — that reservation is what closes the race — while
		# staying out of the set that means "this replica is serving".
		self.assertIn("Draft", CLAIM_HOLDING_STATUSES)
		self.assertNotIn("Draft", GPU_CLAIMING_STATUSES)

	def test_holding_is_serving_plus_draft_and_nothing_else(self):
		self.assertEqual(set(CLAIM_HOLDING_STATUSES) - set(GPU_CLAIMING_STATUSES), {"Draft"})

	def test_a_stopped_replica_holds_nothing(self):
		# The warm tier depends on this: Inactive gives its cards back, and Start re-takes them.
		self.assertNotIn("Inactive", CLAIM_HOLDING_STATUSES)
		self.assertNotIn("Terminated", CLAIM_HOLDING_STATUSES)


if __name__ == "__main__":
	unittest.main()


class TestAStrandedCardIsRecoverable(unittest.TestCase):
	"""Stored ownership can drift where the derived kind could not: a worker dying between the
	status flip and the release leaves a card claimed by a replica that stopped. Nothing would
	ever free it, so the card reads Allocated forever and no placement can take it.

	`release_if_stale` is the repair, run at the moment someone else wants the card — the only
	moment anyone cares whether the claim is real."""

	def stale(self, holder_status, holder="MD-OLD"):
		"""Whether a claim held by a replica in this status gets cleared."""
		from grove.grove.doctype.gpu_claim.gpu_claim import release_if_stale

		values = {("GPU Claim", "mc-x:0", "model_replica"): holder,
		          ("Model Replica", holder, "status"): holder_status}
		deleted = []
		with (
			patch.object(
				frappe, "db", frappe._dict(get_value=lambda dt, n, f, **k: values.get((dt, n, f)))
			),
			patch.object(frappe, "delete_doc", lambda dt, n, **k: deleted.append(n)),
		):
			freed = release_if_stale("mc-x:0")
		return freed, deleted

	def test_a_claim_held_by_a_stopped_replica_is_cleared(self):
		# The exact drift: status says Inactive, the claim says otherwise.
		self.assertEqual(self.stale("Inactive"), (True, ["mc-x:0"]))

	def test_a_claim_held_by_a_torn_down_replica_is_cleared(self):
		self.assertEqual(self.stale("Terminated"), (True, ["mc-x:0"]))

	def test_a_claim_whose_replica_is_gone_entirely_is_cleared(self):
		# get_value on a deleted replica returns None, which is not a holding status.
		self.assertEqual(self.stale(None), (True, ["mc-x:0"]))

	def test_a_genuinely_held_card_is_left_alone(self):
		# Real contention, not drift. Clearing here would hand a live engine's card to a sibling
		# and put two vLLMs on it — the failure the whole claim exists to prevent.
		for status in CLAIM_HOLDING_STATUSES:
			with self.subTest(status):
				self.assertEqual(self.stale(status), (False, []))

	def test_a_claim_nobody_holds_is_not_touched(self):
		# No holder recorded at all: there is nothing to judge, so leave it and let the insert
		# decide. Deleting on a blank read would drop a row a concurrent claim just wrote.
		freed, deleted = self.stale("Active", holder=None)
		self.assertEqual((freed, deleted), (False, []))
