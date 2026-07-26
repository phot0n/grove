# Copyright (c) 2026, Grove and contributors
# See license.txt
"""nvidia-smi parsing. Pure — no site or box needed."""

import unittest

from grove.grove.doctype.machine.machine import _scan_message, parse_nvidia_smi


class TestParseNvidiaSmi(unittest.TestCase):
	def test_multi_gpu_box(self):
		stdout = (
			"0, NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97887, GPU-11111111-2222-3333-4444-555555555555\n"
			"1, NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97887, GPU-66666666-7777-8888-9999-000000000000"
		)
		gpus = parse_nvidia_smi(stdout)
		self.assertEqual(len(gpus), 2)
		self.assertEqual(gpus[0]["gpu_index"], 0)
		self.assertEqual(gpus[0]["gpu_model"], "NVIDIA RTX PRO 6000 Blackwell Workstation Edition")
		self.assertEqual(gpus[0]["vram_gb"], 96)  # 97887 MiB rounds up to the marketed 96 GB
		self.assertTrue(gpus[1]["gpu_uuid"].startswith("GPU-6666"))

	def test_vram_rounds_not_truncates(self):
		# 81559 MiB is an 80 GB A100; truncating would call it 79.
		self.assertEqual(parse_nvidia_smi("0, NVIDIA A100-SXM4-80GB, 81559, GPU-x")[0]["vram_gb"], 80)
		self.assertEqual(parse_nvidia_smi("0, Tesla T4, 15360, GPU-x")[0]["vram_gb"], 15)

	def test_skips_warnings_and_blank_lines(self):
		stdout = "\nWARNING: infoROM is corrupted\n0, Tesla T4, 15360, GPU-x\n\n"
		gpus = parse_nvidia_smi(stdout)
		self.assertEqual(len(gpus), 1)
		self.assertEqual(gpus[0]["gpu_model"], "Tesla T4")

	def test_missing_uuid_column(self):
		gpus = parse_nvidia_smi("0, Tesla T4, 15360")
		self.assertEqual(gpus[0]["gpu_uuid"], "")

	def test_unreadable_memory_does_not_crash(self):
		# nvidia-smi prints [N/A] when a card can't report memory.
		self.assertEqual(parse_nvidia_smi("0, Tesla T4, [N/A], GPU-x")[0]["vram_gb"], 0)

	def test_empty_output(self):
		self.assertEqual(parse_nvidia_smi(""), [])
		self.assertEqual(parse_nvidia_smi(None), [])

	def test_driver_mismatch_yields_no_gpus(self):
		# Real rc=18 output: nvidia-smi writes the error to stdout, not stderr, so the parser
		# must not mistake it for inventory.
		stdout = "Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 580.173"
		self.assertEqual(parse_nvidia_smi(stdout), [])


class TestScanMessage(unittest.TestCase):
	def test_driver_mismatch_explains_the_cause(self):
		message = _scan_message({"stdout": "Failed to initialize NVML: Driver/library version mismatch"})
		self.assertIn("Failed to initialize NVML", message)
		self.assertIn("reboot", message)

	def test_falls_back_through_stderr_then_msg(self):
		self.assertEqual(_scan_message({"stderr": "nvidia-smi: not found"}), "nvidia-smi: not found")
		self.assertEqual(_scan_message({"msg": "non-zero return code"}), "non-zero return code")
		self.assertEqual(_scan_message({}), "nothing at all")


if __name__ == "__main__":
	unittest.main()
