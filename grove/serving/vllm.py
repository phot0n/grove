# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
import math
import shlex

from grove.serving.base import Engine


# vLLM does not fall back to float16 — it raises `Bfloat16 is only supported on GPUs with compute
# capability of at least 8.0` and exits.
BF16_MIN_COMPUTE_CAPABILITY = 8.0
FP8_MIN_COMPUTE_CAPABILITY = 8.9
BF16 = ("bfloat16", "torch.bfloat16", "bf16")


class VllmEngine(Engine):
	"""An image whose entrypoint takes `vllm serve` arguments."""

	# What the routing side assumes when the placement states nothing — never passed to vLLM, so
	# the two can drift as the image tag moves. Drift only costs accuracy in the capacity gate;
	# set max_num_seqs to pin both sides.
	default_concurrency = 1024

	@property
	def repo(self):
		"""The positional argument to `vllm serve` — the S3 mirror when one is set (weights
		stream straight to the GPU), the HF repo otherwise."""
		return self.model.get("weights_s3_uri") or self.model.get("hf_repo") or ""

	@property
	def health_path(self):
		"""vLLM's own liveness endpoint. Needs no api-key, unlike /v1/models."""
		return "/health"

	@property
	def has_api_key(self):
		"""vLLM enforces VLLM_API_KEY, and on-prem that is the only per-engine credential."""
		return True

	@property
	def usable_vram_gb(self):
		"""VRAM vLLM may allocate across the placement's GPUs. 0 when the per-GPU figure is
		unknown."""
		if not self.gpu_vram_gb:
			return 0
		return self.gpu_count * self.gpu_vram_gb * self.gpu_memory_utilization

	@property
	def is_embedding(self):
		"""Pooling model: serves /v1/embeddings, so the chat-only flags are meaningless."""
		return self.model.get("modality") == "embedding"

	@property
	def weight_dtype(self):
		"""What the weights will be served in: the override, or what the repo asks for. `auto` is
		not a third answer — it is vLLM reading `torch_dtype`, which is why the Model carries it."""
		if self.dtype != "auto":
			return self.dtype
		return (self.model.get("torch_dtype") or "").strip()

	@property
	def placement_errors(self):
		"""Why this GPU split cannot start, empty when it can. Checked before a deploy so vLLM does
		not fail minutes in, on the box. A blank Model field skips its check."""
		errors = []
		# Layers need not divide by the pipeline size: get_pp_indices spreads the remainder.
		if self.gpu_count % self.pipeline_parallel_size:
			errors.append(
				f"{self.gpu_count} GPUs do not divide evenly into "
				f"{self.pipeline_parallel_size} pipeline stages."
			)
		heads = self.model.get("attention_heads")
		if heads and heads % self.tensor_parallel_size:
			errors.append(
				f"{self.model_name} has {heads} attention heads, which cannot be sharded across "
				f"tensor-parallel size {self.tensor_parallel_size} — vLLM needs an even split. "
				f"Use a GPU count whose tensor-parallel size divides {heads}."
			)
		# Capability, not capacity: a card can have room for the weights and still be unable to
		# represent them. An unknown on either side skips the check.
		if (
			self.compute_capability
			and self.weight_dtype.lower() in BF16
			and self.compute_capability < BF16_MIN_COMPUTE_CAPABILITY
		):
			errors.append(
				f"{self.model_name} is served in {self.weight_dtype}, which needs a GPU of compute "
				f"capability {BF16_MIN_COMPUTE_CAPABILITY} or better — these are "
				f"{self.compute_capability}. Set dtype to float16 on the deployment (or on one "
				f"replica) to run it here, or place it on a newer card."
			)
		if (
			self.compute_capability
			and self.kv_cache_dtype.startswith("fp8")
			and self.compute_capability < FP8_MIN_COMPUTE_CAPABILITY
		):
			errors.append(
				f"An fp8 KV cache needs a GPU of compute capability "
				f"{FP8_MIN_COMPUTE_CAPABILITY} or better — these are {self.compute_capability}. "
				f"Clear kv_cache_dtype, or place this on a newer card."
			)
		if self.usable_vram_gb and self.weights_gb > self.usable_vram_gb:
			errors.append(
				f"{self.model_name}'s weights are {self.weights_gb} GB but these GPUs offer "
				f"{self.usable_vram_gb:.1f} GB usable ({self.gpu_count} x {self.gpu_vram_gb} GB at "
				f"gpu-memory-utilization {self.gpu_memory_utilization}) — and the KV cache still "
				f"needs room on top. Add GPUs, or raise gpu-memory-utilization."
			)
		return errors

	@property
	def warmup_request(self):
		"""The smallest real inference this placement can serve, as {path, body} — proof of a
		forward pass under the name the gateway routes on, which /v1/models does not give.

		/v1/completions, not chat: chat needs a tokenizer template, and a base repo without one
		answers 400 on an engine that serves fine. Both paths run the same pipeline."""
		if self.model.get("modality") == "audio":
			# Transcription wants a base64 audio file. Not worth carrying to prove one forward pass.
			return {}
		if self.is_embedding:
			return {"path": "/v1/embeddings", "body": {"model": self.model_name, "input": "ping"}}
		return {
			"path": "/v1/completions",
			"body": {"model": self.model_name, "prompt": "ping", "max_tokens": 1},
		}

	@property
	def args(self):
		"""The flags only, without the positional repo (the run script supplies that)."""
		args = [
			"--served-model-name", self.model_name,
			"--host", self.host,
			"--port", str(self.port),
			"--tensor-parallel-size", str(self.tensor_parallel_size),
			"--gpu-memory-utilization", str(self.gpu_memory_utilization),
			"--max-model-len", str(self.max_model_len),
			# Surfaces cached_tokens so billing can credit prefix-cache hits. Reporting-only.
			"--enable-prompt-tokens-details",
			# vLLM adopts the forwarded X-Request-Id as its request_id, so this logs it and the
			# body carries it. NOT --enable-request-id-headers: the gateway sets the response
			# header, and that flag would echo a duplicate.
			"--enable-log-requests",
			# Generated text, at INFO — so the completion is on the box without DEBUG.
			"--enable-log-outputs",
			# Default leaks the exact vLLM build on every response and SSE frame. Stripped here;
			# the gateway alternative rewrites the streaming hot path.
			"--fingerprint-mode", "none",
		]
		# Learned off a profiled boot; vLLM then skips profiling and sizes the cache to this.
		# --gpu-memory-utilization stays: vLLM ignores it for the cache when this is set, and the
		# placement arithmetic (usable_vram_gb) still reads it.
		if self.kv_cache_memory:
			args += ["--kv-cache-memory", str(self.kv_cache_memory)]
		if self.pipeline_parallel_size > 1:
			args += ["--pipeline-parallel-size", str(self.pipeline_parallel_size)]
		# Left to vLLM until a card cannot run what the repo asks for. `--dtype float16` is the
		# remedy placement_errors names for a pre-Ampere card.
		if self.dtype != "auto":
			args += ["--dtype", self.dtype]
		# fp8 halves the KV cache and buys context on a card short of it.
		if self.kv_cache_dtype != "auto":
			args += ["--kv-cache-dtype", self.kv_cache_dtype]
		if self.max_num_batched_tokens:
			args += ["--max-num-batched-tokens", str(self.max_num_batched_tokens)]
		# Unset means vLLM sizes it off the model and the KV cache it ends up with, which beats
		# any number imposed here. See default_concurrency for what routing assumes then.
		if self.max_num_seqs:
			args += ["--max-num-seqs", str(self.max_num_seqs)]
		# A flag, not VLLM_ATTENTION_BACKEND: that env var is gone in 0.24 and setting it
		# silently left the engine auto-selecting.
		if self.attention_backend != "auto":
			args += ["--attention-backend", self.attention_backend]
		if self.model.get("modality") == "text":
			args.append("--language-model-only")
		if self.model.get("enable_prefix_caching"):
			args.append("--enable-prefix-caching")
		if not self.is_embedding:
			if self.model.get("enable_auto_tool_choice"):
				args.append("--enable-auto-tool-choice")
			if self.model.get("tool_call_parser"):
				args += ["--tool-call-parser", self.model["tool_call_parser"]]
			if self.model.get("thinking") and self.model.get("reasoning_parser"):
				args += ["--reasoning-parser", self.model["reasoning_parser"]]
		if self.is_streaming:
			args += ["--load-format", "runai_streamer", *self.streamer_config_args]
		return args + self.extra_serve_args

	@property
	def streamer_config_args(self):
		"""Streamer tuning. concurrency = ceil(weights / 4 GB), the AWS-benchmarked chunk size;
		distributed lets each TP rank stream its own shard instead of a rank-0 broadcast."""
		config = {}
		if self.weights_gb:
			config["concurrency"] = math.ceil(self.weights_gb / 4)
		if self.tensor_parallel_size > 1:
			config["distributed"] = True
		if not config:
			return []
		return ["--model-loader-extra-config", json.dumps(config, separators=(",", ":"))]

	@property
	def command(self):
		"""Repo + flags, the container's start command. shlex-joined: the streamer's JSON arg
		carries braces a shell would brace-expand."""
		return shlex.join([self.repo, *self.args]) if self.repo else ""

	def env(self, hf_home="", cache_root="", api_key="", hf_token="", streaming_env=None):
		"""vLLM's own variables. Insertion order reproduces the on-prem env file line for line —
		see Engine.env."""
		env = {
			"VLLM_LOGGING_LEVEL": "INFO",
			# Safe on every image: a plain env lookup in every hub version, unlike hf_transfer.
			"HF_HUB_DISABLE_TELEMETRY": "1",
			# vLLM phones home on startup unless told not to.
			"VLLM_NO_USAGE_STATS": "1",
			"SAFETENSORS_LOAD_STRATEGY": "prefetch",
		}
		if self.is_streaming:
			env.update(streaming_env or {})
		if hf_token:
			env["HF_TOKEN"] = hf_token
		if self.allow_long_max_model_len:
			env["VLLM_ALLOW_LONG_MAX_MODEL_LEN"] = "1"
		# Only when the engine gets its own cache. Ansible pre-downloads on a box, so the
		# container never fetches and the fast-transfer knob is moot.
		if hf_home:
			env["HF_HOME"] = hf_home
			env["HF_XET_HIGH_PERFORMANCE"] = "1"
		if cache_root:
			# Compile caches on the placement's durable path so a restart skips torch.compile.
			env["VLLM_CACHE_ROOT"] = cache_root
			env["TRITON_CACHE_DIR"] = f"{cache_root}/triton"
			env["TORCHINDUCTOR_CACHE_DIR"] = f"{cache_root}/torchinductor"
		if api_key:
			env["VLLM_API_KEY"] = api_key
		return env
