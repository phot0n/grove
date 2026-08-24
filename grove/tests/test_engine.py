# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The engine contract itself: the arithmetic every engine shares, the dispatch that picks one, and
what a custom image answers. Pure — an engine takes a plain mapping, so no site and no mocking."""

import unittest

from grove.serving.base import Engine, EngineError, build_engine, engine_class
from grove.serving.custom import CustomEngine
from grove.serving.vllm import VllmEngine

CHAT_MODEL = {"hf_repo": "Qwen/Qwen3-35B", "modality": "text"}


class TestParallelism(unittest.TestCase):
	"""Shared arithmetic: how many GPUs are left to shard across is not an engine's opinion."""

	def engine(self, **tuning):
		tuning.setdefault("port", 8080)
		return VllmEngine("qwen3-35b", dict(CHAT_MODEL), **tuning)

	def test_tensor_parallel_is_gpus_left_after_pipeline_stages(self):
		self.assertEqual(self.engine(gpu_count=8).tensor_parallel_size, 8)
		self.assertEqual(self.engine(gpu_count=8, pipeline_parallel_size=2).tensor_parallel_size, 4)
		self.assertEqual(self.engine(gpu_count=4, pipeline_parallel_size=4).tensor_parallel_size, 1)


class TestEngineDispatch(unittest.TestCase):
	def test_each_kind_resolves_to_its_class(self):
		self.assertIs(engine_class("vllm"), VllmEngine)
		self.assertIs(engine_class("custom"), CustomEngine)

	def test_an_unknown_kind_is_an_error_not_a_silent_default(self):
		# Falling back to vLLM would derive `vllm serve` flags for an image that takes none, and
		# the failure would land minutes later on the box instead of here.
		with self.assertRaises(EngineError):
			engine_class("sglang")

	def test_build_engine_passes_the_placements_tuning(self):
		engine = build_engine("vllm", "qwen3-35b", dict(CHAT_MODEL), port=8081, max_num_seqs=32)
		self.assertIsInstance(engine, VllmEngine)
		self.assertEqual(engine.port, 8081)

	def test_an_unknown_knob_is_refused_rather_than_silently_dropped(self):
		# A flag that never applied is worse than one that failed loudly: the engine runs, and the
		# tuning the operator set is simply absent.
		with self.assertRaises(TypeError):
			build_engine("vllm", "qwen3-35b", dict(CHAT_MODEL), port=8080, nonsense=1)

	def test_every_engine_states_a_default_concurrency(self):
		# The ABC cannot enforce a class attribute, and pathway_sync reads this off the CLASS for
		# route rows it never builds an engine for — a missing one is an AttributeError mid-tick.
		for kind in ("vllm", "custom"):
			with self.subTest(kind):
				self.assertIsInstance(engine_class(kind).default_concurrency, int)

	def test_the_contract_is_abstract(self):
		# Every member on Engine is either abstract or shared arithmetic; instantiating the
		# contract itself must be impossible.
		with self.assertRaises(TypeError):
			Engine("qwen3-35b", {}, port=8080)


class TestCustomEngine(unittest.TestCase):
	"""An image that serves itself. Every answer is an absence, and those absences are exactly what
	the `is_custom_engine` branches used to say."""

	def engine(self, **tuning):
		tuning.setdefault("port", 8080)
		return CustomEngine("nemotron-asr", dict(CHAT_MODEL), **tuning)

	def test_nothing_is_derived_for_it(self):
		engine = self.engine()
		self.assertEqual(engine.repo, "")
		self.assertEqual(engine.args, [])
		self.assertEqual(engine.command, "")

	def test_a_model_with_a_repo_still_gets_no_positional(self):
		# The Model is the route key alone for a custom image. Passing its hf_repo as a positional
		# would hand an argument to an entrypoint that never asked for one.
		self.assertEqual(self.engine().repo, "")

	def test_the_startup_command_is_the_whole_argument_list(self):
		engine = self.engine(startup_command="--http-port 9000 --model-repo /models")
		self.assertEqual(engine.args, ["--http-port", "9000", "--model-repo", "/models"])

	def test_a_quoted_startup_argument_stays_one_argument(self):
		engine = self.engine(startup_command="--flag 'a b c'")
		self.assertEqual(engine.args, ["--flag", "a b c"])

	def test_it_gets_none_of_the_vllm_variables(self):
		env = self.engine().env(hf_home="/data/hf", cache_root="/data/vllm-cache", api_key="k")
		self.assertEqual(env, {"HF_HUB_DISABLE_TELEMETRY": "1", "HF_HOME": "/data/hf"})

	def test_no_placement_rule_is_asserted_against_it(self):
		# The bug this exists for: today Pod.validate early-returns for a custom image and never
		# evaluates these. A shared "neutral" implementation would start failing containers that
		# have been serving fine — vLLM's rules assume vLLM's sharding.
		engine = CustomEngine(
			"nemotron-asr",
			dict(CHAT_MODEL, attention_heads=64, weights_gb=148.0),
			port=8080, gpu_count=6, pipeline_parallel_size=4, gpu_vram_gb=8,
		)
		self.assertEqual(engine.placement_errors, [])

	def test_it_publishes_no_health_path_and_no_warmup(self):
		# A guessed path is worse than no gate: plenty of images 404 whatever we would try.
		self.assertEqual(self.engine().health_path, "")
		self.assertEqual(self.engine().warmup_request, {})

	def test_grove_mints_it_no_key(self):
		# pathway_sync ships the key as the route's internal_key, so minting one would send a
		# bearer to an image that never asked for it.
		self.assertFalse(self.engine().has_api_key)

	def test_its_advertised_capacity_is_unchanged(self):
		# 0 ("no capacity of ours to divide") is arguably the honest number, but changing it would
		# move the route table for every custom placement already running.
		self.assertEqual(CustomEngine.default_concurrency, 1024)


if __name__ == "__main__":
	unittest.main()
