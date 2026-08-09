# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""One source of truth for `vllm serve` arguments. Both placements build them here: a Pod
serves a Model in a container (args → dockerStartCmd) and a Model Deployment serves it under
systemd on an Inference Server (args → the unit's ExecStart). Model-intrinsic flags come
from the Model, per-box tuning from whichever doc owns the placement."""

import frappe

# Model fields the args are derived from — read live, never mirrored onto the placement.
MODEL_FIELDS = (
	"hf_repo", "modality", "enable_prefix_caching",
	"enable_auto_tool_choice", "tool_call_parser", "thinking", "reasoning_parser",
	"attention_heads", "weights_gb", "scheduling_policy",
)

DEFAULT_PORT = 8080
DEFAULT_MAX_MODEL_LEN = 8192
DEFAULT_GPU_MEMORY_UTILIZATION = 0.9
DEFAULT_SCHEDULING_POLICY = "priority"
# What the engine runs concurrently when nobody says otherwise. vLLM's own V1 default happens to
# be this too, but the flag is passed anyway rather than left off: the gateway holds admissions to
# this number (Route.Capacity), and it cannot hold an engine to a default that moves with the
# image tag — which is per Engine Image doc and defaults to `latest`.
DEFAULT_MAX_NUM_SEQS = 1024


class ServeCommand:
	"""The `vllm serve` arguments for one placement of a Model. `model` is a mapping of
	MODEL_FIELDS (read live off the Model); everything else is per-box tuning."""

	def __init__(
		self,
		model_name,
		model,
		port,
		gpu_count=1,
		pipeline_parallel_size=1,
		gpu_vram_gb=None,
		kv_cache_dtype=None,
		gpu_memory_utilization=None,
		max_model_len=None,
		max_num_batched_tokens=None,
		max_num_seqs=None,
		attention_backend=None,
		aliases=None,
		extra_serve_args=None,
		host="0.0.0.0",
	):
		self.model_name = model_name
		self.model = model or {}
		self.port = int(port or DEFAULT_PORT)
		self.gpu_count = int(gpu_count or 1)
		self.pipeline_parallel_size = int(pipeline_parallel_size or 1)
		self.gpu_vram_gb = gpu_vram_gb
		self.kv_cache_dtype = kv_cache_dtype or "auto"
		self.gpu_memory_utilization = gpu_memory_utilization or DEFAULT_GPU_MEMORY_UTILIZATION
		self.max_model_len = int(max_model_len or DEFAULT_MAX_MODEL_LEN)
		# 0 = leave it to vLLM, which sizes it off the model and the KV cache it ends up with.
		# Deliberately not defaulted like max_num_seqs: the right number depends on chunked
		# prefill and the model kind, and nothing reads it back the way the gateway reads the
		# sequence cap.
		self.max_num_batched_tokens = int(max_num_batched_tokens or 0)
		self.max_num_seqs = int(max_num_seqs or DEFAULT_MAX_NUM_SEQS)
		self.attention_backend = attention_backend or "auto"
		self.aliases = (aliases or "").replace(",", " ").split()
		self.extra_serve_args = (extra_serve_args or "").split()
		self.host = host

	@classmethod
	def for_pod(cls, pod):
		"""Container placement: the pod's GPUs split TP x PP, serve_port the port."""
		return cls(
			pod.model,
			_model_row(pod.model),
			port=pod.serve_port,
			gpu_count=pod.gpu_count,
			pipeline_parallel_size=pod.pipeline_parallel_size,
			gpu_vram_gb=pod.gpu_vram_gb,
			kv_cache_dtype=pod.kv_cache_dtype,
			gpu_memory_utilization=pod.gpu_memory_utilization,
			max_model_len=pod.max_model_len,
			max_num_seqs=pod.max_num_seqs,
			attention_backend=pod.attention_backend,
			aliases=pod.aliases,
			extra_serve_args=pod.extra_serve_args,
		)

	@classmethod
	def for_deployment(cls, deployment):
		"""Systemd placement: the pinned GPU rows split TP x PP, on the box-local
		engine_port."""
		return cls(
			deployment.model,
			_model_row(deployment.model),
			port=deployment.engine_port,
			gpu_count=len(deployment.gpus or []),
			pipeline_parallel_size=deployment.pipeline_parallel_size,
			gpu_vram_gb=deployment.gpu_vram_gb,
			kv_cache_dtype=deployment.kv_cache_dtype,
			gpu_memory_utilization=deployment.gpu_memory_utilization,
			max_model_len=deployment.max_model_len,
			max_num_batched_tokens=deployment.max_num_batched_tokens,
			max_num_seqs=deployment.max_num_seqs,
			attention_backend=deployment.attention_backend,
			aliases=deployment.aliases,
			extra_serve_args=deployment.extra_serve_args,
		)

	@property
	def tensor_parallel_size(self):
		"""GPUs left for tensor parallelism once the pipeline stages are carved out —
		vLLM's world size is TP x PP, which has to equal the GPUs on hand."""
		return max(self.gpu_count // self.pipeline_parallel_size, 1)

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
	def weights_gb(self):
		"""Size of the Model's weights, 0 when it hasn't been fetched off the repo yet."""
		return self.model.get("weights_gb") or 0

	@property
	def usable_vram_gb(self):
		"""VRAM vLLM may allocate across the placement's GPUs, 0 when the per-GPU VRAM
		isn't known (on-prem rows unfilled, or the provider's GPU types not fetched)."""
		if not self.gpu_vram_gb:
			return 0
		return self.gpu_count * self.gpu_vram_gb * self.gpu_memory_utilization

	@property
	def repo(self):
		"""HF repo — the positional argument to `vllm serve`."""
		return self.model.get("hf_repo")

	@property
	def scheduling_policy(self):
		"""→ --scheduling-policy. Owned by the Model, so every placement of it queues the
		same way."""
		return self.model.get("scheduling_policy") or DEFAULT_SCHEDULING_POLICY

	@property
	def is_embedding(self):
		"""Pooling model: serves /v1/embeddings, so the chat-only flags are meaningless."""
		return self.model.get("modality") == "embedding"

	@property
	def args(self):
		"""The flags only, without the positional repo (the systemd unit supplies that)."""
		args = [
			"--served-model-name", self.model_name, *self.aliases,
			"--host", self.host,
			"--port", str(self.port),
			"--tensor-parallel-size", str(self.tensor_parallel_size),
			"--gpu-memory-utilization", str(self.gpu_memory_utilization),
			"--max-model-len", str(self.max_model_len),
			# TODO: "priority" only bites once the gateway stamps a per-request `priority` on
			# the body (off the caller's tier) — until then everything arrives at the default
			# priority, which queues exactly like fcfs.
			"--scheduling-policy", self.scheduling_policy,
			# Surfaces usage.prompt_tokens_details.cached_tokens so cached-token accounting
			# works (billable = total - cached). Reporting-only.
			"--enable-prompt-tokens-details",
			# Request-id tracing (native vLLM — no custom image/middleware). vLLM adopts the
			# forwarded X-Request-Id as its request_id (chatcmpl-<id>), so this logs it and the
			# response body id carries it. The response HEADER is set by the gateway, so we
			# deliberately DON'T pass --enable-request-id-headers (it would echo a duplicate).
			"--enable-log-requests",
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
		# Always passed, never left to the image's default — see DEFAULT_MAX_NUM_SEQS.
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
		return args + self.extra_serve_args

	@property
	def command(self):
		"""Repo + flags — the container's start command. Empty when the Model has no repo."""
		return " ".join([self.repo, *self.args]) if self.repo else ""


def _model_row(model_name):
	"""The Model's intrinsic launch config, read live."""
	return frappe.db.get_value("Model", model_name, MODEL_FIELDS, as_dict=True) or {}
