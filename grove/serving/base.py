# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The engine-agnostic contract Pod and Model Replica call through. A concrete engine
(VllmEngine, CustomEngine, ...) implements every abstract member here; neither doctype ever names a
concrete class or branches on engine_kind — engine_class is the one place that dispatch happens.

Nothing here imports frappe. The Model's launch config arrives as a plain mapping
(Model.launch_config) and the placement's tuning as keyword arguments, so an engine can be built
and asserted on without a site.

What is concrete is placement arithmetic and Model facts, not engine policy: how many GPUs are left
for tensor parallelism, and which weights these are, are the same answers whatever serves them."""

import json
import shlex
from abc import ABC, abstractmethod

DEFAULT_PORT = 8080
DEFAULT_MAX_MODEL_LEN = 8192
DEFAULT_GPU_MEMORY_UTILIZATION = 0.9


class EngineError(Exception):
	"""No engine could be built for a placement — its Engine Image names a kind nothing here
	implements. Raised, never thrown: the doctype layer turns it into frappe.throw."""


# Suffixes an operator actually types. A context length is a power of two — 128k is 131072, not
# 128000 — which is the number the repo's config.json declares and the only one vLLM accepts
# without the long-context override.
_CONTEXT_MULTIPLIERS = {"k": 1024, "m": 1024 * 1024}


def parse_context_length(value):
	"""A context length as typed → tokens. `32k` → 32768, `128k` → 131072, `1m` → 1048576; a bare
	number is already tokens. Blank is 0, so the caller picks its own default."""
	text = str(value or "").strip().lower()
	if not text:
		return 0
	multiplier = _CONTEXT_MULTIPLIERS.get(text[-1], 1)
	tokens = _to_int(text[:-1] if multiplier > 1 else text, value) * multiplier
	if tokens <= 0:
		raise EngineError(f"Context length '{value}' has to be positive.")
	return tokens


def _to_int(text, typed):
	try:
		return int(text)
	except ValueError:
		raise EngineError(
			f"'{typed}' is not a context length. Give tokens (131072) or a suffix (32k, 128k, 1m)."
		) from None


class Engine(ABC):
	"""One placement of one Model: what starts it, what environment it needs, what proves it
	serves, and what the routing side may hold it to.

	`model` is the Model's launch config read live; everything else is the per-box tuning off
	whichever doc owns the placement. A Pod and a Model Replica carry the same knobs, so the
	constructor takes them all and an engine ignores the ones it has no use for."""

	# What the ROUTING side assumes this engine runs concurrently when the placement names no
	# number. A class attribute, not a property: pathway_sync reads it off the CLASS for route rows
	# it never builds an engine for. Every concrete engine sets one.
	default_concurrency: int

	def __init__(
		self,
		model_name,
		model,
		port,
		gpu_count=1,
		pipeline_parallel_size=1,
		gpu_vram_gb=None,
		compute_capability=None,
		dtype=None,
		kv_cache_dtype=None,
		gpu_memory_utilization=None,
		max_model_len=None,
		max_num_batched_tokens=None,
		max_num_seqs=None,
		attention_backend=None,
		allow_long_max_model_len=False,
		extra_serve_args=None,
		startup_command=None,
		warmup_path=None,
		warmup_body=None,
		host="0.0.0.0",
	):
		self.model_name = model_name
		self.model = model or {}
		self.port = int(port or DEFAULT_PORT)
		self.gpu_count = int(gpu_count or 1)
		self.pipeline_parallel_size = int(pipeline_parallel_size or 1)
		self.gpu_vram_gb = gpu_vram_gb
		# What the box's weakest card can do, 0 when nothing has scanned it. 0 means "unknown",
		# and an unknown skips the capability checks rather than failing them — the same reading
		# a blank CPU architecture gets.
		self.compute_capability = float(compute_capability or 0)
		self.dtype = dtype or "auto"
		self.kv_cache_dtype = kv_cache_dtype or "auto"
		self.gpu_memory_utilization = gpu_memory_utilization or DEFAULT_GPU_MEMORY_UTILIZATION
		self.max_model_len = parse_context_length(max_model_len) or DEFAULT_MAX_MODEL_LEN
		# 0 = leave it to vLLM, which sizes it off the model and the KV cache it ends up with.
		# Deliberately not defaulted like max_num_seqs: the right number depends on chunked
		# prefill and the model kind, and nothing reads it back the way the gateway reads the
		# sequence cap.
		self.max_num_batched_tokens = int(max_num_batched_tokens or 0)
		# 0 = leave it to vLLM, same as max_num_batched_tokens above. The routing side still needs
		# a number to hold the engine to and assumes default_concurrency.
		self.max_num_seqs = int(max_num_seqs or 0)
		self.attention_backend = attention_backend or "auto"
		self.allow_long_max_model_len = bool(allow_long_max_model_len)
		self.extra_serve_args = shlex.split(extra_serve_args or "")
		# What a custom image is invoked with. Split here rather than at the placement so both
		# planes hand the engine the same raw string the operator typed.
		self.startup_command = shlex.split(startup_command or "")
		# Off the Engine Image, not the placement: which request proves the engine serves is a fact
		# about the image. Parsed here so both planes hand the engine the raw JSON as typed.
		self.warmup_path = warmup_path or ""
		self.warmup_body = json.loads(warmup_body) if warmup_body else {}
		self.host = host

	# --- Placement arithmetic and Model facts -----------------------------------

	@property
	def tensor_parallel_size(self):
		"""GPUs left for tensor parallelism once the pipeline stages are carved out — the world
		size is TP x PP, which has to equal the GPUs on hand."""
		return max(self.gpu_count // self.pipeline_parallel_size, 1)

	@property
	def weights_gb(self):
		"""Size of the Model's weights, 0 when it hasn't been fetched off the repo yet."""
		return self.model.get("weights_gb") or 0

	@property
	def is_streaming(self):
		"""Weights come off the S3 mirror rather than an HF cache, so the bucket's credentials
		belong in the environment and nothing needs pre-downloading."""
		return bool(self.model.get("weights_s3_uri"))

	# --- The contract -----------------------------------------------------------

	@property
	@abstractmethod
	def repo(self):
		"""The positional argument the image is invoked with, or "" when it takes none and its own
		entrypoint is the whole story. Rendered UNQUOTED into the run script, so it is a repo id or
		a URI and never operator free-text."""

	@property
	@abstractmethod
	def args(self):
		"""The flag list that follows the positional, each element rendered as one quoted argument.
		Empty when the image is started with no arguments of ours."""

	@property
	@abstractmethod
	def command(self):
		"""Positional + flags as one shell-safe string — a container's start command. "" when
		nothing is derived. Never the image itself: the placement supplies that."""

	@abstractmethod
	def env(self, hf_home="", cache_root="", api_key="", hf_token="", streaming_env=None):
		"""The environment this engine needs → {"VAR": "value"}. The engine names the variables;
		the PLACEMENT names the paths — a container on a provider volume and a container on a box
		cache in different places, and neither is the engine's to choose. A blank path means the
		placement puts that cache somewhere the engine is not told about, so the variable is left
		out rather than guessed at. streaming_env is the weights bucket's credentials, applied only
		when this engine actually streams from it.

		Insertion order is load-bearing: docker --env-file is line-ordered and the on-prem template
		iterates .items(), so a reorder re-renders the env file and replaces every container."""

	@property
	@abstractmethod
	def placement_errors(self):
		"""Why this GPU split cannot start, [] when it can — checked before a deploy so the engine
		does not fail minutes in, on the box. An engine that cannot judge the shape it is handed
		returns [] rather than inventing a rule; blank shape fields on the Model skip their own
		check rather than block the deploy."""

	@property
	@abstractmethod
	def health_path(self):
		"""The path on the serve port that answers 200 once this engine is up, or "" when it
		publishes none and the provider's own state is the whole status. A placement may override
		it with a path of its own."""

	@property
	@abstractmethod
	def warmup_request(self):
		"""The smallest real inference this engine can serve → {"path": ..., "body": {...}}, or {}
		when nothing is cheap enough to be worth proving. Proof of a forward pass under the name the
		gateway routes on, which a 200 from a health path does not give. Empty turns the step off on
		both planes — the Pod posts it in Python, the box posts it from the play.

		Derived by an engine that knows the surface it serves; read off warmup_path/warmup_body by
		one that does not."""

	@property
	@abstractmethod
	def has_api_key(self):
		"""Whether Grove mints the key this engine serves behind. True means the placement generates
		one and the gateway is handed it as the route's internal key. False means the image owns its
		own auth, and a key of ours would be sent as a bearer to something that never asked for
		one."""


def engine_class(engine_kind):
	"""The Engine for an Engine Image's engine_kind. Add an engine by adding one entry here — Pod,
	Model Replica and pathway_sync never change."""
	from grove.serving.custom import CustomEngine
	from grove.serving.vllm import VllmEngine

	engines = {"vllm": VllmEngine, "custom": CustomEngine}
	cls = engines.get(engine_kind)
	if not cls:
		raise EngineError(f"No engine for engine kind '{engine_kind}'.")
	return cls


def build_engine(engine_kind, model_name, model, **tuning):
	"""One placement's Engine. `tuning` is the placement doc's own knobs plus the Engine Image's —
	an unknown one is a TypeError here rather than a flag that silently never applied."""
	return engine_class(engine_kind)(model_name, model, **tuning)
