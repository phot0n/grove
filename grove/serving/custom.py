# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""An image that serves itself — an ASR container, say. Grove derives no arguments for it and
judges no placement shape: what it needs to run is whatever its own entrypoint does, plus the
operator's Startup Command and Env rows."""

import shlex

from grove.serving.base import Engine


class CustomEngine(Engine):
	"""The image is the whole story. Every answer here is the absence of one, and those absences
	are the point — they are what the four `is_custom_engine` branches used to say."""

	# What the routing side assumes, unchanged from what a custom placement advertises today. Not
	# 0 ("no capacity of ours to divide"), which is arguably the honest number but would move the
	# route table for every custom placement already running.
	default_concurrency = 1024

	@property
	def repo(self):
		"""Nothing positional: the entrypoint already names what it serves."""
		return ""

	@property
	def args(self):
		"""Whatever the operator typed, and only that. Each element is rendered as one quoted
		argument, so a Startup Command cannot reach the shell that starts the container."""
		return list(self.startup_command)

	@property
	def command(self):
		"""The operator's Startup Command, or "" for the image's own entrypoint with no arguments."""
		return shlex.join(self.args) if self.args else ""

	def env(self, hf_home="", cache_root="", api_key="", hf_token="", streaming_env=None):
		"""None of the vLLM variables — whatever this image needs comes from its own Env rows. The
		HF cache is still pointed at the placement's durable path when it has one, because an image
		that happens to use huggingface_hub should not write weights into the container layer."""
		env = {"HF_HUB_DISABLE_TELEMETRY": "1"}
		if hf_home:
			env["HF_HOME"] = hf_home
		return env

	@property
	def placement_errors(self):
		"""None of ours to raise. vLLM's rules — heads dividing by tensor-parallel size, the whole
		model fitting in VRAM — assume an engine that shards and loads the way vLLM does, and this
		one may do neither. Asserting them here would fail a container that has been serving fine."""
		return []

	@property
	def health_path(self):
		"""Unknown unless the placement names one, and a guess is worse than no gate: plenty of
		images 404 the paths a health check would try."""
		return ""

	@property
	def warmup_request(self):
		"""No inference we know how to shape. The image's own readiness is the whole signal."""
		return {}

	@property
	def has_api_key(self):
		"""The image enforces no key of ours, so minting one would send a bearer to something that
		never asked for it."""
		return False
