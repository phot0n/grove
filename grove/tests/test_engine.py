# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The engine contract itself: the arithmetic every engine shares, the dispatch that picks one, and
what a custom image answers. Pure — an engine takes a plain mapping, so no site and no mocking."""

import unittest

from grove.serving.base import (
	DEFAULT_MAX_MODEL_LEN,
	Engine,
	EngineError,
	build_engine,
	engine_class,
	parse_context_length,
)
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


class TestContextLength(unittest.TestCase):
	"""What an operator types in Max Model Len. The point is not having to look up that 128k is
	131072 — a context length is a power of two, so the suffix is 1024, never 1000."""

	def test_a_suffix_is_a_power_of_two(self):
		self.assertEqual(parse_context_length("32k"), 32768)
		self.assertEqual(parse_context_length("64k"), 65536)
		self.assertEqual(parse_context_length("128k"), 131072)
		self.assertEqual(parse_context_length("256k"), 262144)
		self.assertEqual(parse_context_length("1m"), 1048576)

	def test_case_and_spacing_are_not_the_operators_problem(self):
		self.assertEqual(parse_context_length(" 128K "), 131072)

	def test_a_bare_number_is_already_tokens(self):
		# Every placement saved before the suffix existed holds one of these.
		self.assertEqual(parse_context_length(131072), 131072)
		self.assertEqual(parse_context_length("131072"), 131072)

	def test_blank_defers_to_the_caller(self):
		self.assertEqual(parse_context_length(""), 0)
		self.assertEqual(parse_context_length(None), 0)

	def test_nonsense_fails_here_rather_than_on_the_box(self):
		# vLLM would take the flag, refuse to start, and the failure would land minutes later
		# inside a play with nothing naming the cause.
		for typed in ("128kb", "lots", "12x", "-1", "0", "1.5k"):
			with self.subTest(typed), self.assertRaises(EngineError):
				parse_context_length(typed)

	def test_the_engine_serves_what_was_typed(self):
		engine = build_engine("vllm", "qwen3-35b", dict(CHAT_MODEL), port=8080, max_model_len="128k")
		self.assertEqual(engine.max_model_len, 131072)
		self.assertEqual(engine.args[engine.args.index("--max-model-len") + 1], "131072")

	def test_blank_still_lands_on_the_engine_default(self):
		engine = build_engine("vllm", "qwen3-35b", dict(CHAT_MODEL), port=8080)
		self.assertEqual(engine.max_model_len, DEFAULT_MAX_MODEL_LEN)


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

	def test_the_image_can_name_the_request_that_proves_it_serves(self):
		# The one thing a custom image is asked to state: nothing here can shape a request for a
		# surface it does not know, so the Engine Image carries the path and the body.
		engine = self.engine(
			warmup_path="/v1/audio/transcriptions",
			warmup_body='{"model": "nemotron-asr", "language": "en"}',
		)
		self.assertEqual(
			engine.warmup_request,
			{
				"path": "/v1/audio/transcriptions",
				"body": {"model": "nemotron-asr", "language": "en"},
			},
		)

	def test_a_path_with_no_body_posts_an_empty_one(self):
		self.assertEqual(
			self.engine(warmup_path="/ready").warmup_request, {"path": "/ready", "body": {}}
		)

	def test_a_body_with_no_path_is_still_no_warmup(self):
		# Nowhere to post it. Half-configured is off, not a guess at the path.
		self.assertEqual(self.engine(warmup_body='{"input": "ping"}').warmup_request, {})

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
