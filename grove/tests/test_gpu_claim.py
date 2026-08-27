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

from grove.grove.doctype.gpu_claim.gpu_claim import GPUClaim
from grove.grove.doctype.model_replica.model_replica import (
	CLAIM_HOLDING_STATUSES,
	GPU_CLAIMING_STATUSES,
)


def named(inference_server, gpu_index):
	doc = SimpleNamespace(inference_server=inference_server, gpu_index=gpu_index, name=None)
	GPUClaim.autoname(doc)
	return doc.name


class TestTheNameIsTheClaim(unittest.TestCase):
	def test_a_card_names_itself_once(self):
		self.assertEqual(named("inf-blackwell", 0), "inf-blackwell:0")

	def test_two_replicas_wanting_one_card_want_one_name(self):
		# The entire concurrency design in one assertion: whatever order two placements arrive
		# in, they are competing to insert the same primary key, and only one insert can win.
		self.assertEqual(named("inf-blackwell", 0), named("inf-blackwell", 0))

	def test_different_cards_on_one_box_are_different_names(self):
		self.assertNotEqual(named("inf-blackwell", 0), named("inf-blackwell", 1))

	def test_the_same_index_on_two_boxes_does_not_collide(self):
		# Every box counts its cards from 0, so the box has to be in the name or card 0 would be
		# a single fleet-wide resource.
		self.assertNotEqual(named("inf-blackwell", 0), named("test-aws-2-inf", 0))

	def test_a_string_index_still_names_an_int(self):
		# A whitelisted call and a JS dialog both hand over strings. "0" and 0 must be one card,
		# or a claim could be taken twice under two spellings.
		self.assertEqual(named("inf-blackwell", "0"), named("inf-blackwell", 0))

	def test_the_separator_cannot_occur_in_a_server_name(self):
		# Server names are DNS labels and carry dashes (`inf-blackwell`), so a dash separator
		# would make the split ambiguous. A colon cannot appear in one.
		self.assertNotIn(":", "inf-blackwell")
		self.assertIn(":", named("inf-blackwell", 0))


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
