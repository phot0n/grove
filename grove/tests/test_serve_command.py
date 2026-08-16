# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""ServeCommand argument assembly. Pure — the Model row is passed in, so no site needed."""

import unittest
import unittest.mock
from types import SimpleNamespace

from grove import serve_command
from grove.serve_command import DEFAULT_MAX_NUM_SEQS, ServeCommand

CHAT_MODEL = {
	"hf_repo": "Qwen/Qwen3-35B",
	"modality": "text",
	"enable_prefix_caching": True,
	"enable_auto_tool_choice": True,
	"tool_call_parser": "hermes",
	"thinking": True,
	"reasoning_parser": "qwen3",
}


def serve(model=None, **tuning):
	tuning.setdefault("port", 8080)
	return ServeCommand("qwen3-35b", model if model is not None else dict(CHAT_MODEL), **tuning)


class TestParallelism(unittest.TestCase):
	def test_tensor_parallel_is_gpus_left_after_pipeline_stages(self):
		self.assertEqual(serve(gpu_count=8).tensor_parallel_size, 8)
		self.assertEqual(serve(gpu_count=8, pipeline_parallel_size=2).tensor_parallel_size, 4)
		self.assertEqual(serve(gpu_count=4, pipeline_parallel_size=4).tensor_parallel_size, 1)

	def test_pipeline_flag_only_when_used(self):
		self.assertNotIn("--pipeline-parallel-size", serve(gpu_count=8).args)
		args = serve(gpu_count=8, pipeline_parallel_size=2).args
		self.assertEqual(args[args.index("--pipeline-parallel-size") + 1], "2")
		self.assertEqual(args[args.index("--tensor-parallel-size") + 1], "4")

	def test_heads_must_divide_by_tensor_parallel_size(self):
		model = dict(CHAT_MODEL, attention_heads=64)
		self.assertEqual(serve(model, gpu_count=8).placement_errors, [])  # 64 / 8 ✓
		errors = serve(model, gpu_count=6).placement_errors  # 64 heads on 6 GPUs ✗
		self.assertEqual(len(errors), 1)
		self.assertIn("64 attention heads", errors[0])

	def test_heads_checked_against_derived_tensor_parallel_size(self):
		# 6 GPUs alone can't shard 64 heads, but PP=2 leaves TP=3 — still not a divisor.
		model = dict(CHAT_MODEL, attention_heads=64)
		self.assertTrue(serve(model, gpu_count=6, pipeline_parallel_size=2).placement_errors)
		# 8 GPUs with PP=2 leaves TP=4, which divides 64.
		self.assertEqual(serve(model, gpu_count=8, pipeline_parallel_size=2).placement_errors, [])

	def test_gpus_must_divide_into_pipeline_stages(self):
		errors = serve(gpu_count=6, pipeline_parallel_size=4).placement_errors
		self.assertIn("do not divide evenly", errors[0])

	def test_layers_need_not_divide_by_pipeline_stages(self):
		# vLLM's get_pp_indices spreads the remainder across stages (43 over 3 → 15/15/13).
		model = dict(CHAT_MODEL, hidden_layers=43, attention_heads=64)
		self.assertEqual(serve(model, gpu_count=3, pipeline_parallel_size=3).placement_errors, [])

	def test_blank_shape_skips_checks(self):
		self.assertEqual(serve(gpu_count=6).placement_errors, [])  # no heads on Model


class TestVramFit(unittest.TestCase):
	# DeepSeek-V4-Flash's real figures: 148 GB of weights, 64 heads.
	BIG = dict(CHAT_MODEL, weights_gb=148.0, attention_heads=64)

	def test_weights_larger_than_gpus_is_rejected(self):
		errors = serve(self.BIG, gpu_count=2, gpu_vram_gb=80).placement_errors  # 144 usable
		self.assertEqual(len(errors), 1)
		self.assertIn("148", errors[0])

	def test_weights_that_fit_pass(self):
		self.assertEqual(serve(self.BIG, gpu_count=4, gpu_vram_gb=80).placement_errors, [])

	def test_gpu_memory_utilization_shrinks_the_budget(self):
		# 2 x 80 at 0.95 = 152 GB usable — fits; at 0.9 it's 144 and does not.
		self.assertEqual(
			serve(self.BIG, gpu_count=2, gpu_vram_gb=80, gpu_memory_utilization=0.95).placement_errors, []
		)
		self.assertTrue(serve(self.BIG, gpu_count=2, gpu_vram_gb=80).placement_errors)

	def test_unknown_vram_skips_the_check(self):
		self.assertEqual(serve(self.BIG, gpu_count=1).placement_errors, [])  # no gpu_vram_gb

	def test_unfetched_weights_skip_the_check(self):
		model = dict(CHAT_MODEL, attention_heads=64)  # no weights_gb
		self.assertEqual(serve(model, gpu_count=8, gpu_vram_gb=8).placement_errors, [])


class TestServeCommand(unittest.TestCase):
	def test_chat_model_flags(self):
		args = serve(gpu_count=2, max_model_len=32768).args
		self.assertEqual(args[:3], ["--served-model-name", "qwen3-35b", "--host"])
		for flag, value in (
			("--port", "8080"),
			("--tensor-parallel-size", "2"),
			("--max-model-len", "32768"),
			("--tool-call-parser", "hermes"),
			("--reasoning-parser", "qwen3"),
		):
			self.assertEqual(args[args.index(flag) + 1], value, flag)
		for flag in ("--language-model-only", "--enable-prefix-caching", "--enable-auto-tool-choice"):
			self.assertIn(flag, args)

	def test_defaults_when_tuning_blank(self):
		args = serve().args
		self.assertEqual(args[args.index("--max-model-len") + 1], "8192")
		self.assertEqual(args[args.index("--gpu-memory-utilization") + 1], "0.9")
		self.assertEqual(args[args.index("--tensor-parallel-size") + 1], "1")
		# Blank tuning → vLLM's own sizing. Never --dtype: the weight dtype is the repo's to
		# declare, and passing one is how a bf16 checkpoint gets silently served as fp16.
		for flag in ("--dtype", "--kv-cache-dtype", "--max-num-batched-tokens", "--scheduling-policy"):
			self.assertNotIn(flag, args, flag)

	def test_the_engine_build_is_not_advertised_to_callers(self):
		# vLLM defaults system_fingerprint to its exact version and build hash, and puts it on
		# every response AND every streaming frame. Passed on every placement, embedding included:
		# the field is on the response shape, not on the kind of model serving it.
		for model in (dict(CHAT_MODEL), {"hf_repo": "x", "modality": "embedding"}):
			with self.subTest(model["modality"]):
				args = serve(model=model).args
				self.assertEqual(args[args.index("--fingerprint-mode") + 1], "none")

	def test_an_unstated_sequence_cap_is_left_to_vllm(self):
		# Nothing is imposed when the placement names no number: vLLM sizes the cap off the model
		# and the KV cache it actually got, which beats a figure chosen from the control plane.
		# The routing side still needs one and assumes DEFAULT_MAX_NUM_SEQS — an assumption, not a
		# contract, and deliberately so.
		self.assertNotIn("--max-num-seqs", serve().args)

	def test_a_stated_sequence_cap_pins_both_sides(self):
		# Setting it is how a placement that needs the number held exactly gets the engine and the
		# capacity gate onto the same one.
		args = serve(max_num_seqs=64).args
		self.assertEqual(args[args.index("--max-num-seqs") + 1], "64")

	def test_kv_cache_dtype_and_batch_caps_when_set(self):
		args = serve(kv_cache_dtype="fp8", max_num_batched_tokens=8192, max_num_seqs=64).args
		self.assertEqual(args[args.index("--kv-cache-dtype") + 1], "fp8")
		self.assertEqual(args[args.index("--max-num-batched-tokens") + 1], "8192")
		self.assertEqual(args[args.index("--max-num-seqs") + 1], "64")

	def test_embedding_modality_drops_chat_flags(self):
		args = serve(dict(CHAT_MODEL, modality="embedding")).args
		for flag in ("--enable-auto-tool-choice", "--tool-call-parser", "--reasoning-parser"):
			self.assertNotIn(flag, args)
		self.assertNotIn("--language-model-only", args)  # text-only flag, not a pooling one
		self.assertIn("--enable-prefix-caching", args)  # not chat-only

	def test_thinking_off_drops_reasoning_parser(self):
		args = serve(dict(CHAT_MODEL, thinking=False)).args
		self.assertNotIn("--reasoning-parser", args)

	def test_aliases_and_extra_args(self):
		args = serve(aliases="old-name, older-name", extra_serve_args="--kv-cache-dtype fp8").args
		self.assertEqual(args[:4], ["--served-model-name", "qwen3-35b", "old-name", "older-name"])
		self.assertEqual(args[-2:], ["--kv-cache-dtype", "fp8"])  # appended verbatim, last

	def test_command_is_repo_then_args(self):
		command = serve().command
		self.assertTrue(command.startswith("Qwen/Qwen3-35B --served-model-name qwen3-35b "))

	def test_command_empty_without_repo(self):
		self.assertEqual(serve(dict(CHAT_MODEL, hf_repo=None)).command, "")


class TestStreamingFromS3(unittest.TestCase):
	"""A Model with an S3 mirror serves it as the positional and loads via the runai
	streamer — weights go S3 → GPU, no HF download."""

	S3 = dict(CHAT_MODEL, weights_s3_uri="s3://grove-weights/models/Qwen--Qwen3-35B", weights_gb=67.0)

	def test_the_mirror_replaces_the_repo(self):
		self.assertEqual(serve(dict(self.S3)).repo, "s3://grove-weights/models/Qwen--Qwen3-35B")
		self.assertEqual(serve().repo, "Qwen/Qwen3-35B")

	def test_streamer_flags_only_for_a_mirrored_model(self):
		args = serve(dict(self.S3)).args
		self.assertEqual(args[args.index("--load-format") + 1], "runai_streamer")
		self.assertNotIn("--load-format", serve().args)

	def test_concurrency_is_sized_from_the_weights(self):
		# ceil(67 / 4 GB chunks) = 17 — the AWS-benchmarked sizing. TP=1 → no distributed.
		args = serve(dict(self.S3)).args
		self.assertEqual(args[args.index("--model-loader-extra-config") + 1], '{"concurrency":17}')

	def test_tp_over_one_streams_each_rank_its_own_shard(self):
		args = serve(dict(self.S3), gpu_count=2).args
		self.assertEqual(
			args[args.index("--model-loader-extra-config") + 1],
			'{"concurrency":17,"distributed":true}',
		)

	def test_unfetched_weights_still_stream_on_defaults(self):
		args = serve(dict(self.S3, weights_gb=None)).args
		self.assertIn("--load-format", args)
		self.assertNotIn("--model-loader-extra-config", args)

	def test_the_streamer_json_survives_a_shell(self):
		# The Pod path hands `command` to a shell-ish parser, and bash brace-expands a bare
		# {a,b} into two words — shlex.join is what quotes it.
		command = serve(dict(self.S3), gpu_count=2).command
		self.assertIn("'{\"concurrency\":17,\"distributed\":true}'", command)

	def test_extra_serve_args_still_come_last(self):
		# The operator's escape hatch outranks the derived flags, streamer included.
		args = serve(dict(self.S3), extra_serve_args="--load-format auto").args
		self.assertEqual(args[-2:], ["--load-format", "auto"])


class TestAttentionBackend(unittest.TestCase):
	def build(self, **kwargs):
		return ServeCommand("qwen3-32b", CHAT_MODEL, port=8080, **kwargs).args

	def test_passed_as_a_flag(self):
		# NOT VLLM_ATTENTION_BACKEND: vLLM 0.24 removed that env var, so the engine
		# auto-selected regardless of what the deployment asked for.
		args = self.build(attention_backend="FLASHINFER")
		self.assertEqual(args[args.index("--attention-backend") + 1], "FLASHINFER")

	def test_auto_is_left_to_vllm(self):
		self.assertNotIn("--attention-backend", self.build(attention_backend="auto"))
		self.assertNotIn("--attention-backend", self.build())



class TestPlacementsPassTheirTuning(unittest.TestCase):
	"""Both placements read their tuning off their own doc. Asserted on --max-num-seqs because
	the gateway now holds admissions to the same number: a placement that dropped it would leave
	the gateway capping at a figure the engine is not running at."""

	def args_for(self, classmethod_name, doc):
		with unittest.mock.patch.object(serve_command, "_model_row", return_value=dict(CHAT_MODEL)):
			return getattr(ServeCommand, classmethod_name)(doc).args

	def test_a_pod_serves_with_its_own_max_num_seqs(self):
		pod = SimpleNamespace(
			model="qwen3-35b", serve_port=8080, gpu_count=1, pipeline_parallel_size=1,
			gpu_vram_gb=80, kv_cache_dtype=None, gpu_memory_utilization=0.9, max_model_len=0,
			max_num_seqs=32, attention_backend=None, aliases="", extra_serve_args="",
		)
		args = self.args_for("for_pod", pod)
		self.assertEqual(args[args.index("--max-num-seqs") + 1], "32")

	def test_a_deployment_serves_with_its_own_max_num_seqs(self):
		deployment = SimpleNamespace(
			model="qwen3-35b", engine_port=8080, gpus=[], pipeline_parallel_size=1,
			gpu_vram_gb=80, kv_cache_dtype=None, gpu_memory_utilization=0.9, max_model_len=0,
			max_num_batched_tokens=0, max_num_seqs=32, attention_backend=None,
			aliases="", extra_serve_args="",
		)
		args = self.args_for("for_deployment", deployment)
		self.assertEqual(args[args.index("--max-num-seqs") + 1], "32")


if __name__ == "__main__":
	unittest.main()
