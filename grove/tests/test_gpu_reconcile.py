# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""What a scan does to the cards already on record.

Pure — `plan_reconcile` needs no site, and it is the half worth pinning hardest: a card's row is
where its claim lives, so a rule that deletes a row instead of updating it takes a running
replica's GPU with it and hands the same card to the next placement."""

import unittest
from types import SimpleNamespace

from grove.grove.doctype.machine.machine import is_placeholder_device_id, plan_reconcile

UUID = "GPU-70d10f6c-268d-493a-572d-aeb575e8a866"
OTHER_UUID = "GPU-902a29d1-5c94-5f97-6c22-ce812ed7b58c"


def card(name, device_id, gpu_index):
	return SimpleNamespace(name=name, device_id=device_id, gpu_index=gpu_index)


def scan(device_id, gpu_index, model="Tesla T4"):
	return {"device_id": device_id, "gpu_index": gpu_index, "gpu_model": model, "vram_gb": 16}


class TestPlanReconcile(unittest.TestCase):
	def test_a_placeholder_is_filled_in_not_replaced(self):
		# The whole point. Every card in the fleet carries a bare index until its box is scanned;
		# treating that as an identity makes the first honest scan look like one card removed and
		# another added, and the removal takes `held_by` with it.
		rows = [card("gpu-a", "0", 0)]
		upgrades, inserts, stale = plan_reconcile(rows, [scan(UUID, 0)])
		self.assertEqual([(c.name, r["device_id"]) for c, r in upgrades], [("gpu-a", UUID)])
		self.assertEqual((inserts, stale), ([], []))

	def test_a_swap_keeps_both_rows(self):
		# Two real UUIDs that changed slots are still the same two cards. Matching by index here
		# would move each claim onto the other card's silicon.
		rows = [card("gpu-a", UUID, 0), card("gpu-b", OTHER_UUID, 1)]
		upgrades, inserts, stale = plan_reconcile(rows, [scan(UUID, 1), scan(OTHER_UUID, 0)])
		self.assertEqual({c.name for c, _r in upgrades}, {"gpu-a", "gpu-b"})
		self.assertEqual((inserts, stale), ([], []))

	def test_a_card_the_box_gained_is_inserted(self):
		upgrades, inserts, stale = plan_reconcile([], [scan(UUID, 0)])
		self.assertEqual((upgrades, stale), ([], []))
		self.assertEqual([row["device_id"] for row in inserts], [UUID])

	def test_a_card_the_box_no_longer_reports_is_stale(self):
		rows = [card("gpu-a", UUID, 0), card("gpu-b", OTHER_UUID, 1)]
		_upgrades, _inserts, stale = plan_reconcile(rows, [scan(UUID, 0)])
		self.assertEqual([c.name for c in stale], ["gpu-b"])

	def test_a_placeholder_whose_slot_is_empty_is_not_rescued(self):
		# The lenient rule must not reach past its own slot: card 1 is genuinely gone, and pairing
		# it with the only card reported would pin a replica to silicon that is not there.
		rows = [card("gpu-a", "0", 0), card("gpu-b", "1", 1)]
		upgrades, inserts, stale = plan_reconcile(rows, [scan(UUID, 0)])
		self.assertEqual([c.name for c, _r in upgrades], ["gpu-a"])
		self.assertEqual(inserts, [])
		self.assertEqual([c.name for c in stale], ["gpu-b"])

	def test_a_box_that_still_has_no_uuid_to_give_does_not_churn(self):
		# An AWS-seeded box re-read from the provider still reports indexes. Same placeholder,
		# matched exactly, so nothing is created or destroyed.
		rows = [card("gpu-a", "0", 0)]
		upgrades, inserts, stale = plan_reconcile(rows, [scan("0", 0)])
		self.assertEqual([c.name for c, _r in upgrades], ["gpu-a"])
		self.assertEqual((inserts, stale), ([], []))


class TestARentedBoxIsRehosted(unittest.TestCase):
	"""Stop/start an EC2 instance and it comes back on another host, holding a different physical
	card at the same index — AWS's own remedy for a sick GPU is exactly that. The UUID is then the
	transient half and the slot is the durable one, which is the inverse of bare metal."""

	def test_a_new_card_at_a_known_slot_keeps_the_row(self):
		# The row is where `held_by` and every replica's pin live. Replacing it strands a replica
		# on a record that no longer exists: it cannot be saved, and a deploy would hand vLLM
		# fewer devices than the tensor-parallel size its child rows still declare.
		rows = [card("gpu-a", UUID, 0)]
		upgrades, inserts, stale = plan_reconcile(
			rows, [scan(OTHER_UUID, 0)], slot_is_identity=True
		)
		self.assertEqual([(c.name, r["device_id"]) for c, r in upgrades], [("gpu-a", OTHER_UUID)])
		self.assertEqual((inserts, stale), ([], []))

	def test_bare_metal_prunes_the_same_input(self):
		# The negative control, and the reason this is a property of the box. On hardware we own,
		# a UUID that stops answering means the card was pulled — absorbing a stranger into its
		# row would quietly repoint a replica at silicon nobody chose.
		rows = [card("gpu-a", UUID, 0)]
		upgrades, inserts, stale = plan_reconcile(rows, [scan(OTHER_UUID, 0)])
		self.assertEqual(upgrades, [])
		self.assertEqual([row["device_id"] for row in inserts], [OTHER_UUID])
		self.assertEqual([c.name for c in stale], ["gpu-a"])

	def test_each_slot_keeps_its_own_row(self):
		# Both cards changed underneath. Slot 0 must not land on slot 1's row, or two replicas
		# swap hardware without either one moving.
		rows = [card("gpu-a", UUID, 0), card("gpu-b", OTHER_UUID, 1)]
		fresh_a, fresh_b = "GPU-1111", "GPU-2222"
		upgrades, inserts, stale = plan_reconcile(
			rows, [scan(fresh_a, 0), scan(fresh_b, 1)], slot_is_identity=True
		)
		self.assertEqual(
			sorted((c.name, r["device_id"]) for c, r in upgrades),
			[("gpu-a", fresh_a), ("gpu-b", fresh_b)],
		)
		self.assertEqual((inserts, stale), ([], []))

	def test_a_slot_the_box_no_longer_reports_is_still_pruned(self):
		# Slot identity is not a licence to keep everything: an instance resized to fewer GPUs
		# really has fewer, and a card kept here would be offered to a placement forever.
		rows = [card("gpu-a", UUID, 0), card("gpu-b", OTHER_UUID, 1)]
		_upgrades, inserts, stale = plan_reconcile(
			rows, [scan("GPU-1111", 0)], slot_is_identity=True
		)
		self.assertEqual(inserts, [])
		self.assertEqual([c.name for c in stale], ["gpu-b"])

	def test_an_unchanged_box_is_matched_by_uuid_not_slot(self):
		# Nothing moved: the exact pass takes both, so the slot pass never runs and a card that
		# genuinely swapped slots on a cloud box still keeps its own row.
		rows = [card("gpu-a", UUID, 0), card("gpu-b", OTHER_UUID, 1)]
		upgrades, inserts, stale = plan_reconcile(
			rows, [scan(UUID, 1), scan(OTHER_UUID, 0)], slot_is_identity=True
		)
		self.assertEqual(
			sorted((c.name, r["device_id"]) for c, r in upgrades),
			[("gpu-a", UUID), ("gpu-b", OTHER_UUID)],
		)
		self.assertEqual((inserts, stale), ([], []))


class TestPlaceholders(unittest.TestCase):
	def test_only_bare_digits_are_placeholders(self):
		self.assertTrue(is_placeholder_device_id("0"))
		self.assertTrue(is_placeholder_device_id("11"))
		self.assertFalse(is_placeholder_device_id(UUID))
		self.assertFalse(is_placeholder_device_id("MIG-41b3359c-1234-5678-90ab-cdef12345678"))
		self.assertFalse(is_placeholder_device_id(""))
		self.assertFalse(is_placeholder_device_id(None))


if __name__ == "__main__":
	unittest.main()
