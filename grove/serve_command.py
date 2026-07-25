# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""One source of truth for `vllm serve` arguments. Both placements build them here: a Pod
serves a Model in a container (args → dockerStartCmd) and a Model Deployment serves it under
systemd on an Inference Server (args → the unit's ExecStart). Model-intrinsic flags come
from the Model, per-box tuning from whichever doc owns the placement."""

import frappe

# Model fields the args are derived from — read live, never mirrored onto the placement.
MODEL_FIELDS = (
	"hf_repo", "is_embedding", "quantization", "modality", "enable_prefix_caching",
	"enable_auto_tool_choice", "tool_call_parser", "thinking", "reasoning_parser",
)

DEFAULT_PORT = 8080
DEFAULT_MAX_MODEL_LEN = 8192
DEFAULT_GPU_MEMORY_UTILIZATION = 0.9


class ServeCommand:
	"""The `vllm serve` arguments for one placement of a Model. `model` is a mapping of
	MODEL_FIELDS (read live off the Model); everything else is per-box tuning."""

	def __init__(
		self,
		model_name,
		model,
		port,
		tensor_parallel_size=1,
		dtype=None,
		gpu_memory_utilization=None,
		max_model_len=None,
		aliases=None,
		extra_serve_args=None,
		host="0.0.0.0",
	):
		self.model_name = model_name
		self.model = model or {}
		self.port = int(port or DEFAULT_PORT)
		self.tensor_parallel_size = int(tensor_parallel_size or 1)
		self.dtype = dtype or "auto"
		self.gpu_memory_utilization = gpu_memory_utilization or DEFAULT_GPU_MEMORY_UTILIZATION
		self.max_model_len = int(max_model_len or DEFAULT_MAX_MODEL_LEN)
		self.aliases = (aliases or "").replace(",", " ").split()
		self.extra_serve_args = (extra_serve_args or "").split()
		self.host = host

	@classmethod
	def for_pod(cls, pod):
		"""Container placement: GPU count is the tensor-parallel size, serve_port the port."""
		return cls(
			pod.model,
			_model_row(pod.model),
			port=pod.serve_port,
			tensor_parallel_size=pod.gpu_count,
			dtype=pod.dtype,
			gpu_memory_utilization=pod.gpu_memory_utilization,
			max_model_len=pod.max_model_len,
			aliases=pod.aliases,
			extra_serve_args=pod.extra_serve_args,
		)

	@classmethod
	def for_deployment(cls, deployment):
		"""Systemd placement: tensor-parallel over the pinned GPU rows, on the box-local
		engine_port."""
		return cls(
			deployment.model,
			_model_row(deployment.model),
			port=deployment.engine_port,
			tensor_parallel_size=deployment.tensor_parallel_size,
			dtype=deployment.dtype,
			gpu_memory_utilization=deployment.gpu_memory_utilization,
			max_model_len=deployment.max_model_len,
			aliases=deployment.aliases,
			extra_serve_args=deployment.extra_serve_args,
		)

	@property
	def repo(self):
		"""HF repo — the positional argument to `vllm serve`."""
		return self.model.get("hf_repo")

	@property
	def is_embedding(self):
		"""Pooling model: serves /v1/embeddings, so the chat-only flags are meaningless."""
		return bool(self.model.get("is_embedding"))

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
			# Surfaces usage.prompt_tokens_details.cached_tokens so cached-token accounting
			# works (billable = total - cached). Reporting-only.
			"--enable-prompt-tokens-details",
			# Request-id tracing (native vLLM — no custom image/middleware). vLLM adopts the
			# forwarded X-Request-Id as its request_id (chatcmpl-<id>), so this logs it and the
			# response body id carries it. The response HEADER is set by the gateway, so we
			# deliberately DON'T pass --enable-request-id-headers (it would echo a duplicate).
			"--enable-log-requests",
		]
		if self.dtype != "auto":
			args += ["--dtype", self.dtype]
		if self.model.get("quantization"):
			args += ["--quantization", self.model["quantization"]]
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
