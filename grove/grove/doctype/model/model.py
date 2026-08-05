# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import requests

import frappe
from frappe.model.document import Document

from grove.utils import slugify

HF_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"
HF_MODEL_URL = "https://huggingface.co/api/models/{repo}"
# Repo root listing, with a size per file. The limit is well past any real shard count.
HF_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/main?limit=1000"

# Bytes per parameter, for the Weights dtype override only — the shipped size is measured off
# the files themselves rather than costed out like this.
DTYPE_BYTES = {
	"float32": 4, "bfloat16": 2, "float16": 2, "fp8": 1, "int8": 1, "int4": 0.5,
}
# HF reports a packed quantization as its container type: AWQ/GPTQ pack eight 4-bit weights
# into one int32, so for those repos the counts describe containers, not parameters.
PACKED_DTYPES = {"I16", "U16", "I32", "U32"}


class Model(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attention_heads: DF.Int
		display_name: DF.Data
		enable_auto_tool_choice: DF.Check
		enable_prefix_caching: DF.Check
		hf_repo: DF.Data | None
		hidden_layers: DF.Int
		is_embedding: DF.Check
		modality: DF.Literal["text", "multimodal"]
		published: DF.Check
		quantization: DF.Literal["", "awq", "awq_marlin", "gptq", "gptq_marlin", "fp8"]
		reasoning_parser: DF.Data | None
		scheduling_policy: DF.Literal["priority", "fcfs"]
		thinking: DF.Check
		tool_call_parser: DF.Data | None
		weights_dtype: DF.Literal["", "bfloat16", "float16", "float32", "fp8", "int8", "int4"]
		weights_gb: DF.Float
	# end: auto-generated types

	def autoname(self):
		"""Name = slugged Display Name. The name is the client-facing model id (what
		clients send as `model` and what routes are keyed by), so it's set once at insert
		— editing Display Name later does NOT rename it and break live clients."""
		self.name = slugify(self.display_name)
		if not self.name:
			frappe.throw("Display Name must contain at least one letter or digit.")

	def validate(self):
		# The read-only-when-published rule, enforced past the client.
		if is_scheduling_policy_frozen(self.get_doc_before_save(), self):
			frappe.throw(
				"Scheduling Policy is frozen while the model is published — it is baked into "
				"the running engines. Tear its placements down to change it."
			)

		# `published` means "reachable" — it tracks whether a live route exists, and is
		# never a manual claim. It is not an access gate: access is granted per user, via
		# Grove User Group or Grove User.
		if self.published and not has_active_deployment(self.name):
			self.published = 0

	@property
	def repo_id(self):
		"""The repo alone. A GGUF repo publishes a dozen quantizations of the same model, so
		vLLM is pointed at one of them with `unsloth/Qwen3-0.6B-GGUF:Q4_K_M` — a ref the HF
		API does not take."""
		return (self.hf_repo or "").split(":")[0]

	@property
	def gguf_quant(self):
		"""The quantization named after the colon, blank for a safetensors repo."""
		return (self.hf_repo or "").partition(":")[2]

	@frappe.whitelist()
	def fetch_architecture(self):
		"""Button: read the head and layer counts off the repo's config.json, so the
		parallelism checks have real numbers instead of hand-typed ones."""
		if not self.hf_repo:
			frappe.throw("Set the HF Repo first — that's what's read.")
		config = self.get_hf_config()
		# Multimodal repos nest the language model's shape; the top level describes the
		# whole thing (vision tower included) and is what vLLM shards for --language-model-only.
		shape = config.get("text_config") or config.get("llm_config") or {}
		heads = shape.get("num_attention_heads") or config.get("num_attention_heads")
		layers = shape.get("num_hidden_layers") or config.get("num_hidden_layers")
		if not (heads and layers):
			frappe.throw(f"{self.hf_repo}'s config.json has no head/layer count to read.")
		values = {"attention_heads": heads, "hidden_layers": layers}
		if weights_gb := self.get_weights_gb():
			values["weights_gb"] = weights_gb
		self.db_set(values)
		frappe.msgprint(
			f"{self.hf_repo}: {heads} attention heads, {layers} layers"
			+ (f", {values['weights_gb']} GB of weights." if "weights_gb" in values else ".")
		)
		return values

	def get_hf_config(self):
		"""The repo's config.json — its architecture."""
		return self._hf_json(HF_CONFIG_URL)

	def get_weights_gb(self):
		"""Size of the weights in GB — decimal, to match how GPU VRAM is quoted. Normally what
		the repo's own weights weigh; with a Weights dtype set, its parameters re-costed at
		that precision instead."""
		if self.weights_dtype:
			return self.get_rescaled_weights_gb()
		return self.get_shipped_weights_gb()

	def get_shipped_weights_gb(self):
		"""What the repo's weights actually weigh: its top-level safetensors shards, added up
		as they are on disk — or the single GGUF file, for a repo ref that names a quantization.
		None when it publishes neither.

		Measured, not costed out from parameter counts. HF reports a count per dtype, but a
		packed quantization reports its container type — GLM-5.2-AWQ-INT4 comes back as 726
		billion I32, which prices at 2959 GB against a real 474 GB. Subfolders are skipped on
		purpose: that is where a repo keeps other quantizations of the same weights, and adding
		those would charge for the model more than once."""
		suffix = f"{self.gguf_quant}.gguf" if self.gguf_quant else ".safetensors"
		total_bytes = sum(
			entry.get("size") or 0
			for entry in self._hf_json(HF_TREE_URL)
			if entry.get("type") == "file" and is_root_weights_file(entry.get("path", ""), suffix)
		)
		return round(total_bytes / 1_000_000_000, 2) or None

	def get_rescaled_weights_gb(self):
		"""The repo's parameters re-costed at the chosen Weights dtype — for a repo served at a
		precision it does not ship in, e.g. vLLM quantizing a bf16 repo to fp8. Refused for a
		repo that already ships quantized: its counts are packed containers, so there is no
		parameter count left to re-cost."""
		parameters = (self._hf_json(HF_MODEL_URL).get("safetensors") or {}).get("parameters") or {}
		if packed := PACKED_DTYPES.intersection(parameters):
			frappe.throw(
				f"{self.hf_repo} already ships quantized — its weights are packed as "
				f"{', '.join(sorted(packed))}, so there is no parameter count to re-cost at "
				f"{self.weights_dtype}. Clear Weights dtype to measure the repo as it is."
			)
		if not parameters:
			return None
		total_bytes = sum(parameters.values()) * DTYPE_BYTES[self.weights_dtype]
		return round(total_bytes / 1_000_000_000, 2)

	def _hf_json(self, url_template):
		"""Fetch JSON from Hugging Face. Sends the site's HF token when there is one — gated
		repos 401 without it, as do repos that don't exist (HF doesn't distinguish)."""
		token = frappe.conf.get("hf_token")
		headers = {"Authorization": f"Bearer {token}"} if token else {}
		try:
			response = requests.get(
				url_template.format(repo=self.repo_id), headers=headers, timeout=30
			)
		except requests.RequestException as e:
			frappe.throw(f"Could not reach Hugging Face: {e}")
		if response.status_code in (401, 403, 404):
			frappe.throw(
				f"Hugging Face returned {response.status_code} for {self.hf_repo} — the repo is "
				"gated, private or misspelled. Gated repos need hf_token in the site config."
			)
		if not response.ok:
			frappe.throw(f"Hugging Face returned {response.status_code} for {self.hf_repo}.")
		return response.json()


def is_root_weights_file(path, suffix):
	"""A weights file the repo serves from — one at the root, not one in a subfolder. The
	listing is requested non-recursively, so this is a second line of defence: a repo keeps
	its other quantizations in subfolders, and counting those bills the same model twice."""
	return path.endswith(suffix) and "/" not in path


def is_scheduling_policy_frozen(before, after):
	"""True when a save would move the Scheduling Policy of a model that is already
	serving. The live engines were started with the stored policy, so a new one would name
	a --scheduling-policy nothing is running. `before` is None on insert — nothing live yet
	— and the stored `published` is what counts, since after.published is recomputed."""
	return bool(before and before.published and before.scheduling_policy != after.scheduling_policy)


def has_active_deployment(model, exclude=None):
	"""True if `model` has a live deployment: >=1 Active Model Deployment (on-prem) OR a
	Running standalone Pod (cloud). `exclude` drops one deployment name (used from Model
	Deployment.on_trash, where the row still exists in the DB during delete)."""
	filters = {"model": model, "status": "Active"}
	if exclude:
		filters["name"] = ("!=", exclude)
	if frappe.db.get_all("Model Deployment", filters=filters, limit=1):
		return True
	return bool(frappe.db.get_all("Pod", filters={"model": model, "status": "Running"}, limit=1))


def sync_published(model, exclude=None):
	"""Recompute Model.published to reflect whether it has a live deployment.
	Called whenever a deployment's status changes (deploy / teardown / broken),
	since that's what makes the model reachable. Written via db.set_value so it
	skips validate (no recursion) and is cheap."""
	if not model or not frappe.db.exists("Model", model):
		return
	want = 1 if has_active_deployment(model, exclude=exclude) else 0
	if frappe.db.get_value("Model", model, "published") != want:
		frappe.db.set_value("Model", model, "published", want)
