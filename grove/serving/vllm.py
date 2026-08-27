# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""`vllm serve` arguments, and the one request that proves the engine they started can serve. Both
placements build them here: a Pod serves a Model in a container (command → dockerStartCmd) and a
Model Replica serves it in a container on a box (repo + args → the rendered run script).
Model-intrinsic flags come from the Model, per-box tuning from whichever doc owns the placement."""

import json
import math
import shlex

from grove.serving.base import Engine


class VllmEngine(Engine):
	"""An image whose entrypoint takes `vllm serve` arguments, so a placement derives them from
	the Model."""

	# Not passed to vLLM: a placement that states nothing gets vLLM's own sizing, which reads the
	# model and the KV cache it actually ended up with and is a better number than one imposed
	# from here.
	#
	# So this is an assumption, not a contract, and the two can drift: vLLM's V1 default is 1024
	# today but moves with the image tag, which is per Engine Image and defaults to `latest`.
	# Drift only costs accuracy in the capacity gate — the engine queues what it cannot run, and a
	# placement that needs the number held exactly should set max_num_seqs, which pins both sides.
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
		"""VRAM vLLM may allocate across the placement's GPUs, 0 when the per-GPU VRAM
		isn't known (on-prem rows unfilled, or the provider's GPU types not fetched)."""
		if not self.gpu_vram_gb:
			return 0
		return self.gpu_count * self.gpu_vram_gb * self.gpu_memory_utilization

	@property
	def is_embedding(self):
		"""Pooling model: serves /v1/embeddings, so the chat-only flags are meaningless."""
		return self.model.get("modality") == "embedding"

	@property
	def placement_errors(self):
		"""Why this GPU split cannot start, empty when it can. Checked before a deploy so
		vLLM does not fail minutes in, on the box. Shape fields left blank on the Model skip
		their check rather than block the deploy."""
		errors = []
		# Layers need not divide by the pipeline size — vLLM's get_pp_indices spreads the
		# remainder across stages. Only the TP head split is a hard requirement.
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
		"""The smallest real inference this placement can serve, as {path, body} — proof the engine
		runs a forward pass under the name the gateway routes on, which a 200 from /v1/models does
		not give. Empty when the surface costs more to assert than the assertion is worth.

		/v1/completions rather than /v1/chat/completions: chat needs a tokenizer chat template, and
		a base repo without one answers 400 — a config answer, not a GPU one, on an engine that
		serves fine. Both paths tokenize, schedule, prefill, decode once and detokenize."""
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
			# Surfaces usage.prompt_tokens_details.cached_tokens so cached-token accounting
			# works (billable = total - cached). Reporting-only.
			"--enable-prompt-tokens-details",
			# Request-id tracing (native vLLM — no custom image/middleware). vLLM adopts the
			# forwarded X-Request-Id as its request_id (chatcmpl-<id>), so this logs it and the
			# response body id carries it. The response HEADER is set by the gateway, so we
			# deliberately DON'T pass --enable-request-id-headers (it would echo a duplicate).
			"--enable-log-requests",
			# Logs the generated text alongside the request. vLLM emits both this and the
			# received-request line at INFO, so the completion is on the box without DEBUG.
			"--enable-log-outputs",
			# vLLM defaults system_fingerprint to "full", which is its exact version and build
			# hash — `vllm-0.24.0-bf54a486` — on every response and on EVERY streaming frame.
			# That hands a customer the engine build to look up known issues against, for a field
			# no caller of ours uses. Stripped at the source: the alternative was rewriting each
			# SSE frame in the gateway's body filter, which is a mutation of the streaming hot
			# path to remove one string.
			"--fingerprint-mode", "none",
		]
		if self.pipeline_parallel_size > 1:
			args += ["--pipeline-parallel-size", str(self.pipeline_parallel_size)]
		# Weight dtype is left to vLLM (it reads the repo's config.json); only the KV cache is
		# worth overriding per box — fp8 halves it and buys context on a card that is short of it.
		if self.kv_cache_dtype != "auto":
			args += ["--kv-cache-dtype", self.kv_cache_dtype]
		if self.max_num_batched_tokens:
			args += ["--max-num-batched-tokens", str(self.max_num_batched_tokens)]
		# Only when the placement states one. Unset means vLLM sizes it off the model and the KV
		# cache it ends up with, which is a better number than any we would impose — see
		# default_concurrency for what the routing side assumes in that case.
		if self.max_num_seqs:
			args += ["--max-num-seqs", str(self.max_num_seqs)]
		# A flag, not VLLM_ATTENTION_BACKEND: that env var is gone in vLLM 0.24 (nothing in
		# the package reads it), so setting it silently left the engine auto-selecting.
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
		"""Streamer tuning: concurrency = ceil(weights / 4 GB chunks) — the AWS-benchmarked
		figure; distributed lets each TP rank stream its own shard instead of a rank-0
		broadcast. Empty when nothing needs saying."""
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
		"""Repo + flags — the container's start command. Empty when the Model has no repo.
		shlex-joined: the streamer's JSON arg carries braces a shell would brace-expand."""
		return shlex.join([self.repo, *self.args]) if self.repo else ""

	def env(self, hf_home="", cache_root="", api_key="", hf_token="", streaming_env=None):
		"""vLLM's own variables. Insertion order reproduces the on-prem env file line for line —
		see Engine.env for why that matters."""
		env = {
			"VLLM_LOGGING_LEVEL": "INFO",
			# Telemetry is off for every image: huggingface_hub reads this with a plain env lookup
			# in every version, so unlike hf_transfer it cannot crash an image without extras.
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
		# Only when the placement hands the engine its own HF cache. On a box Ansible pre-downloads
		# with the host venv, so the container never fetches and the fast-transfer knob is moot.
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
