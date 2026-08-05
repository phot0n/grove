# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""How a Model sizes its weights. Pure — the Hugging Face reads are stubbed with real payloads
captured from the repos below, so no site or network."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.model.model import HF_TREE_URL, Model

# Real `safetensors.parameters` maps, off https://huggingface.co/api/models/<repo>.
PARAMETERS = {
	# Ships bf16 — nothing packed, so it can be re-costed at another precision.
	"Qwen/Qwen2.5-3B-Instruct": {"BF16": 3_085_938_688},
	# Ships fp8, with bf16 for the parts that stay wide.
	"Qwen/Qwen3.6-27B-FP8": {"BF16": 3_083_727_792, "F8_E4M3": 24_699_207_680},
	# AWQ int4: the I32 count is packed containers, not parameters.
	"cyankiwi/GLM-5.2-AWQ-INT4": {"BF16": 27_199_429_632, "I32": 726_130_491_392, "F32": 19_456},
}
# Real shard totals, off the repo tree. What the weights actually weigh.
SHIPPED_GB = {"Qwen/Qwen3.6-27B-FP8": 30.87, "cyankiwi/GLM-5.2-AWQ-INT4": 474.22}


def model(repo, weights_dtype=""):
	"""A stand-in Model carrying just what the sizing reads."""
	doc = SimpleNamespace(hf_repo=repo, weights_dtype=weights_dtype)
	doc.repo_id = Model.repo_id.fget(doc)
	doc.gguf_quant = Model.gguf_quant.fget(doc)
	doc.get_weights_gb = Model.get_weights_gb.__get__(doc, SimpleNamespace)
	doc.get_shipped_weights_gb = Model.get_shipped_weights_gb.__get__(doc, SimpleNamespace)
	doc.get_rescaled_weights_gb = Model.get_rescaled_weights_gb.__get__(doc, SimpleNamespace)
	doc._hf_json = lambda url, _repo=repo: (
		tree(_repo) if url == HF_TREE_URL else {"safetensors": {"parameters": PARAMETERS[_repo]}}
	)
	return doc


def tree(repo, shards=4):
	"""A repo root listing: the weights split over shards, plus a config and a subfolder
	holding another quantization of the same model."""
	each = round(SHIPPED_GB[repo] * 1_000_000_000 / shards)
	return [
		{"type": "file", "path": "config.json", "size": 1400},
		{"type": "directory", "path": "fp8"},
		*[
			{"type": "file", "path": f"model-0000{i}.safetensors", "size": each}
			for i in range(1, shards + 1)
		],
	]


class TestShippedWeights(unittest.TestCase):
	def test_the_shards_are_measured_not_costed_from_parameter_counts(self):
		# The I32 count on this repo is packed containers: pricing it per dtype gives 2959 GB.
		self.assertAlmostEqual(model("cyankiwi/GLM-5.2-AWQ-INT4").get_weights_gb(), 474.22, places=1)

	def test_an_fp8_repo_measures_to_its_real_size(self):
		self.assertAlmostEqual(model("Qwen/Qwen3.6-27B-FP8").get_weights_gb(), 30.87, places=1)

	def test_another_quantization_in_a_subfolder_is_not_counted_twice(self):
		listing = tree("Qwen/Qwen3.6-27B-FP8")
		listing.append({"type": "file", "path": "int4/model-00001.safetensors", "size": 10**10})
		doc = model("Qwen/Qwen3.6-27B-FP8")
		doc._hf_json = lambda url: listing
		self.assertAlmostEqual(doc.get_weights_gb(), 30.87, places=1)

	def test_a_repo_with_no_safetensors_reports_nothing(self):
		doc = model("Qwen/Qwen3.6-27B-FP8")
		doc._hf_json = lambda url: [{"type": "file", "path": "pytorch_model.bin", "size": 10**10}]
		self.assertIsNone(doc.get_weights_gb())


class TestGgufWeights(unittest.TestCase):
	"""A GGUF repo ships every quantization of one model side by side, so `repo:QUANT` names
	the single file that is served. Sizing has to follow that ref, not the repo."""

	# Root listing of https://huggingface.co/unsloth/Qwen3-0.6B-GGUF, trimmed to four quants.
	TREE = [
		{"type": "file", "path": "config.json", "size": 752},
		{"type": "file", "path": "Qwen3-0.6B-Q4_K_M.gguf", "size": 396_705_472},
		{"type": "file", "path": "Qwen3-0.6B-Q4_K_S.gguf", "size": 383_270_592},
		{"type": "file", "path": "Qwen3-0.6B-UD-Q4_K_XL.gguf", "size": 405_372_608},
		{"type": "file", "path": "Qwen3-0.6B-Q8_0.gguf", "size": 639_447_744},
	]

	def gguf(self, ref):
		doc = model(ref)
		doc._hf_json = lambda url: self.TREE
		return doc

	def test_only_the_named_quantization_is_measured(self):
		# Not the 1.82 GB the four quantizations weigh together.
		self.assertAlmostEqual(self.gguf("unsloth/Qwen3-0.6B-GGUF:Q4_K_M").get_weights_gb(), 0.4, places=2)

	def test_a_longer_name_ending_the_same_way_is_not_picked_up(self):
		self.assertAlmostEqual(self.gguf("unsloth/Qwen3-0.6B-GGUF:Q8_0").get_weights_gb(), 0.64, places=2)

	def test_a_quantization_the_repo_does_not_publish_reports_nothing(self):
		self.assertIsNone(self.gguf("unsloth/Qwen3-0.6B-GGUF:Q3_K_M").get_weights_gb())

	def test_the_hugging_face_reads_drop_the_quantization_suffix(self):
		# The ref is vLLM's; the HF API only knows the repo.
		doc = model("unsloth/Qwen3-0.6B-GGUF:Q4_K_M")
		self.assertEqual(doc.repo_id, "unsloth/Qwen3-0.6B-GGUF")
		self.assertEqual(model("Qwen/Qwen2.5-3B-Instruct").repo_id, "Qwen/Qwen2.5-3B-Instruct")
		self.assertEqual(model("Qwen/Qwen2.5-3B-Instruct").gguf_quant, "")


class TestRescaledWeights(unittest.TestCase):
	def test_a_bf16_repo_re_costs_at_fp8(self):
		# 3.086e9 parameters at one byte each.
		doc = model("Qwen/Qwen2.5-3B-Instruct", weights_dtype="fp8")
		self.assertAlmostEqual(doc.get_weights_gb(), 3.09, places=1)

	def test_the_same_repo_at_bf16_is_twice_that(self):
		doc = model("Qwen/Qwen2.5-3B-Instruct", weights_dtype="bfloat16")
		self.assertAlmostEqual(doc.get_weights_gb(), 6.17, places=1)

	def test_an_already_packed_repo_is_refused_rather_than_guessed_at(self):
		# Its counts are int32 containers — there is no parameter count left to re-cost.
		doc = model("cyankiwi/GLM-5.2-AWQ-INT4", weights_dtype="fp8")
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
			with self.assertRaises(frappe.ValidationError):
				doc.get_weights_gb()


if __name__ == "__main__":
	unittest.main()
