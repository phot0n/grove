# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Deriving a card type from whatever a source called it.

Pure — `type_name_from` needs no site, which is the half of `resolve` worth pinning hardest: it is
what collapses three sources onto one record without anybody writing an alias by hand."""

import unittest

from grove.grove.doctype.gpu_type.gpu_type import type_name_from


class TestDerivingTheName(unittest.TestCase):
	def test_the_three_spellings_of_one_t4_collapse(self):
		# The actual rows in this fleet: nvidia-smi says `Tesla T4`, AWS says `T4`. Before this
		# they were two different cards to every filter.
		self.assertEqual(type_name_from("Tesla T4"), "T4")
		self.assertEqual(type_name_from("T4"), "T4")
		self.assertEqual(type_name_from("NVIDIA T4"), "T4")

	def test_the_vendor_word_is_dropped_wherever_it_appears(self):
		self.assertEqual(type_name_from("NVIDIA L40S"), "L40S")
		self.assertEqual(type_name_from("L40S"), "L40S")

	def test_a_suffix_it_cannot_judge_is_kept(self):
		# `80GB HBM3` might be a memory configuration or a different card, and nothing here can
		# tell. Keeping it is the safe half of the trade — an alias row is how it reaches `H100`,
		# and a wrong guess would silently merge two different cards into one type.
		self.assertEqual(type_name_from("NVIDIA H100 80GB HBM3"), "H100 80GB HBM3")

	def test_a_mig_slice_is_not_its_parent(self):
		# A slice has a fraction of the memory. Resolving it to the parent would offer a replica
		# 80 GB that does not exist.
		parent = type_name_from("NVIDIA A100-SXM4-80GB")
		slice_ = type_name_from("NVIDIA A100-SXM4-80GB MIG 1g.10gb")
		self.assertNotEqual(parent, slice_)
		self.assertIn("MIG", slice_)

	def test_blank_derives_nothing(self):
		self.assertEqual(type_name_from(""), "")
		self.assertEqual(type_name_from(None), "")

	def test_deriving_is_stable(self):
		# Called on every scan of every box: the same string must always land on the same record,
		# or a re-scan would mint a duplicate type and split a fleet's cards across both.
		self.assertEqual(type_name_from("Tesla T4"), type_name_from("Tesla T4"))


if __name__ == "__main__":
	unittest.main()
