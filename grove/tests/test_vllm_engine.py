# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""VllmEngine argument assembly. Pure — the Model row is passed in, so no site needed."""

import unittest

from grove.serving.vllm import VllmEngine

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
	return VllmEngine("qwen3-35b", model if model is not None else dict(CHAT_MODEL), **tuning)


class TestVllmPlacementErrors(unittest.TestCase):
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


class TestVllmArgs(unittest.TestCase):
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
		# The routing side still needs one and assumes default_concurrency — an assumption, not a
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

	def test_a_quoted_extra_arg_stays_one_argument(self):
		# A JSON value is one argument. Split on spaces it became five, each re-quoted by
		# shlex.join into a fragment vLLM reads as its own flag.
		kwargs = '{"thinking": true, "reasoning_effort": "high"}'
		args = serve(extra_serve_args=f"--default-chat-template-kwargs '{kwargs}'").args
		self.assertEqual(args[-2:], ["--default-chat-template-kwargs", kwargs])

	def test_command_is_repo_then_args(self):
		command = serve().command
		self.assertTrue(command.startswith("Qwen/Qwen3-35B --served-model-name qwen3-35b "))

	def test_command_empty_without_repo(self):
		self.assertEqual(serve(dict(CHAT_MODEL, hf_repo=None)).command, "")

	def test_the_whole_command_is_what_the_fleet_is_already_running(self):
		# This test exists because the engine split moved this code between files. Every live Pod
		# and Model Deployment stores this string; a flag that merely REORDERS re-renders the run
		# script on the box, which replaces the container and drops in-flight requests. The literal
		# below was taken from the pre-split ServeCommand, not from the code it now guards.
		self.assertEqual(
			serve(gpu_count=2, max_model_len=32768).command,
			"Qwen/Qwen3-35B --served-model-name qwen3-35b --host 0.0.0.0 --port 8080 "
			"--tensor-parallel-size 2 --gpu-memory-utilization 0.9 --max-model-len 32768 "
			"--enable-prompt-tokens-details --enable-log-requests --enable-log-outputs "
			"--fingerprint-mode none --language-model-only --enable-prefix-caching "
			"--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3",
		)


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
		return VllmEngine("qwen3-32b", CHAT_MODEL, port=8080, **kwargs).args

	def test_passed_as_a_flag(self):
		# NOT VLLM_ATTENTION_BACKEND: vLLM 0.24 removed that env var, so the engine
		# auto-selected regardless of what the deployment asked for.
		args = self.build(attention_backend="FLASHINFER")
		self.assertEqual(args[args.index("--attention-backend") + 1], "FLASHINFER")

	def test_auto_is_left_to_vllm(self):
		self.assertNotIn("--attention-backend", self.build(attention_backend="auto"))
		self.assertNotIn("--attention-backend", self.build())


class TestVllmEnv(unittest.TestCase):
	"""What the engine needs in its environment, and where the placement's paths go."""

	def test_the_on_prem_key_order_is_the_env_file_line_order(self):
		# The bug this guards: docker --env-file is line-ordered and the on-prem template iterates
		# .items(), so reordering re-renders every vllm-<slug>.env, which fires `recreate vllm
		# container` and replaces every engine in the fleet. This is the order the boxes hold.
		env = serve(allow_long_max_model_len=True).env(hf_token="hf_secret")
		self.assertEqual(
			list(env),
			[
				"VLLM_LOGGING_LEVEL",
				"HF_HUB_DISABLE_TELEMETRY",
				"VLLM_NO_USAGE_STATS",
				"HF_TOKEN",
				"VLLM_ALLOW_LONG_MAX_MODEL_LEN",
			],
		)

	def test_a_blank_path_omits_its_variable_rather_than_guessing(self):
		# On a box the Jinja template writes the cache dirs and the role resolves the key, so the
		# engine is told neither — inventing a path here would point vLLM somewhere nothing mounts.
		env = serve().env()
		for absent in ("HF_HOME", "VLLM_CACHE_ROOT", "TRITON_CACHE_DIR", "VLLM_API_KEY"):
			self.assertNotIn(absent, env, absent)

	def test_a_placement_that_owns_the_caches_gets_them_all(self):
		env = serve().env(hf_home="/data/hf", cache_root="/data/vllm-cache", api_key="k")
		self.assertEqual(env["HF_HOME"], "/data/hf")
		self.assertEqual(env["VLLM_CACHE_ROOT"], "/data/vllm-cache")
		self.assertEqual(env["TRITON_CACHE_DIR"], "/data/vllm-cache/triton")
		self.assertEqual(env["TORCHINDUCTOR_CACHE_DIR"], "/data/vllm-cache/torchinductor")
		self.assertEqual(env["VLLM_API_KEY"], "k")

	def test_the_fast_transfer_knob_rides_the_hf_cache(self):
		# Only where the engine fetches its own weights. On a box Ansible pre-downloads with the
		# host venv, and adding a variable there re-renders the env file for the whole fleet.
		self.assertIn("HF_XET_HIGH_PERFORMANCE", serve().env(hf_home="/data/hf"))
		self.assertNotIn("HF_XET_HIGH_PERFORMANCE", serve().env())

	def test_bucket_credentials_only_reach_an_engine_that_streams(self):
		creds = {"AWS_ACCESS_KEY_ID": "AKIA"}
		streaming = dict(CHAT_MODEL, weights_s3_uri="s3://grove-weights/x")
		self.assertIn("AWS_ACCESS_KEY_ID", serve(streaming).env(streaming_env=creds))
		# The bug this replaces: the Pod path decided by string-matching "runai_streamer" in the
		# stored serve command, which an operator could also type into extra_serve_args.
		self.assertNotIn("AWS_ACCESS_KEY_ID", serve().env(streaming_env=creds))


class TestWarmupRequest(unittest.TestCase):
	"""The one request that proves an engine serves. Both placements post it — the Pod path in
	Python, the Model Deployment path as an Ansible extra-var — so it is built once, here."""

	def test_a_generative_model_is_asked_for_one_token(self):
		request = serve().warmup_request
		self.assertEqual(request["path"], "/v1/completions")
		self.assertEqual(request["body"]["max_tokens"], 1)

	def test_completions_not_chat_completions(self):
		# Chat needs a tokenizer chat template. A base repo has none and answers 400 — a config
		# answer, not a GPU one, which would fail warmup on an engine that serves fine.
		self.assertNotIn("chat", serve().warmup_request["path"])

	def test_an_embedding_model_is_asked_to_embed(self):
		request = serve(dict(CHAT_MODEL, modality="embedding")).warmup_request
		self.assertEqual(request["path"], "/v1/embeddings")
		self.assertEqual(request["body"]["input"], "ping")
		self.assertNotIn("max_tokens", request["body"])

	def test_the_model_asked_for_is_the_one_the_gateway_routes_on(self):
		# pathway_sync publishes `deploy:<Model docname>`, which is the first --served-model-name.
		# An alias or the hf_repo here would prove an engine serves under a name nothing routes to.
		request = serve(aliases="old-name").warmup_request
		self.assertEqual(request["body"]["model"], "qwen3-35b")

	def test_a_vllm_image_derives_its_own_and_ignores_the_images_warmup(self):
		# The fields exist for an image whose surface we cannot know. vLLM's is known, and taking a
		# typed-in path here would let one silently replace a request built from the Model.
		request = serve(warmup_path="/ready", warmup_body='{"input": "ping"}').warmup_request
		self.assertEqual(request["path"], "/v1/completions")

	def test_audio_has_nothing_cheap_to_prove(self):
		# Transcription wants a base64 audio file; an empty request turns the step off on both paths.
		self.assertEqual(serve(dict(CHAT_MODEL, modality="audio")).warmup_request, {})


if __name__ == "__main__":
	unittest.main()
