# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Compile Cache registry: how raw S3 listings group into one row per cache key. Pure —
the listings are passed in, no site and no bucket."""

import unittest

from grove.grove.doctype.compile_cache.compile_cache import entries_from_keys


def obj(key, size=1_000_000):
	return {"Key": key, "Size": size}


PREFIX = "compile-cache/ab12cd34ef56ab78/NVIDIA_L40S/tp2/Qwen--Qwen3-35B"


class TestEntriesFromKeys(unittest.TestCase):
	def test_artifacts_group_into_one_entry_per_cache_key(self):
		entries = entries_from_keys([
			obj(f"{PREFIX}/torch_compile_cache/x/graph.py", 2_000_000),
			obj(f"{PREFIX}/triton/kernel.cubin", 3_000_000),
		])
		[(key, stats)] = entries.items()
		self.assertEqual(key, ("ab12cd34ef56ab78", "NVIDIA_L40S", 2, "Qwen--Qwen3-35B"))
		self.assertEqual(stats, {"objects": 2, "bytes": 5_000_000})

	def test_axes_split_entries(self):
		entries = entries_from_keys([
			obj(f"{PREFIX}/a"),
			obj("compile-cache/ab12cd34ef56ab78/NVIDIA_L40S/tp4/Qwen--Qwen3-35B/a"),
			obj("compile-cache/ffffffffffffffff/NVIDIA_L40S/tp2/Qwen--Qwen3-35B/a"),
		])
		self.assertEqual(len(entries), 3)

	def test_keys_that_do_not_parse_are_skipped_not_guessed(self):
		entries = entries_from_keys([
			obj("models/Qwen--Qwen3-35B/model.safetensors"),  # the weights half of the bucket
			obj("compile-cache/deep/NVIDIA_L40S/tpX/model/a"),  # tp not a number
			obj("compile-cache/short/key"),  # no artifact path
			obj(f"{PREFIX}/a"),
		])
		self.assertEqual(len(entries), 1)

	def test_a_missing_size_counts_as_zero(self):
		entries = entries_from_keys([{"Key": f"{PREFIX}/a"}])
		[stats] = entries.values()
		self.assertEqual(stats["bytes"], 0)


if __name__ == "__main__":
	unittest.main()
