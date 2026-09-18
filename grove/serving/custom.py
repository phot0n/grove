# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import shlex

from grove.serving.base import Engine


class CustomEngine(Engine):
	"""Almost every answer here is the absence of one, and those absences are what the four
	`is_custom_engine` branches used to say."""

	# Not 0 ("no capacity of ours to divide"), which is the honest number but would move the route
	# table for every custom placement already running.
	default_concurrency = 1024

	@property
	def repo(self):
		"""Nothing positional: the entrypoint already names what it serves."""
		return ""

	@property
	def args(self):
		"""Whatever the operator typed. Each element is one quoted argument, so a Startup Command
		cannot reach the shell that starts the container."""
		return list(self.startup_command)

	@property
	def command(self):
		"""The operator's Startup Command, or "" for the image's own entrypoint with no arguments."""
		return shlex.join(self.args) if self.args else ""

	def env(self, hf_home="", cache_root="", api_key="", hf_token="", streaming_env=None):
		"""None of the vLLM variables — this image's needs come from its own Env rows. The HF cache
		still points at the durable path, so an image that does use huggingface_hub is not writing
		weights into the container layer."""
		env = {"HF_HUB_DISABLE_TELEMETRY": "1"}
		if hf_home:
			env["HF_HOME"] = hf_home
		return env

	@property
	def placement_errors(self):
		"""None of ours to raise. vLLM's rules assume an engine that shards and loads the way vLLM
		does, and this one may do neither."""
		return []

	@property
	def health_path(self):
		"""A guess is worse than no gate: plenty of images 404 the paths a check would try."""
		return ""

	@property
	def warmup_request(self):
		"""What the Engine Image says proves it serves. Nothing here can shape a request for a
		surface it does not know, so unset means the health gate is the whole proof."""
		if not self.warmup_path:
			return {}
		return {"path": self.warmup_path, "body": self.warmup_body}

	@property
	def has_api_key(self):
		return False
