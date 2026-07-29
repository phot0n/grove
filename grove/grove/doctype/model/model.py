# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import requests

import frappe
from frappe.model.document import Document

from grove.utils import slugify

HF_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"
HF_MODEL_URL = "https://huggingface.co/api/models/{repo}"

# Bytes per parameter for the dtypes HF reports in a repo's safetensors index. Quantized
# repos mix them (e.g. FP8 weights with BF16 norms), so the total is summed per dtype.
DTYPE_BYTES = {
	"F64": 8, "I64": 8,
	"F32": 4, "I32": 4, "U32": 4,
	"BF16": 2, "F16": 2, "I16": 2, "U16": 2,
	"F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1, "I8": 1, "U8": 1, "BOOL": 1,
	"F4": 0.5, "NF4": 0.5,
}
_UNKNOWN_DTYPE_BYTES = 2  # widest common case, so an unseen dtype errs toward "too big"


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
		gated: DF.Check
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
		"""Size of the weights in GB, summed per dtype off the repo's safetensors index.
		None when the repo publishes no index (older or non-safetensors repos). Decimal GB
		to match how GPU VRAM is quoted."""
		parameters = (self._hf_json(HF_MODEL_URL).get("safetensors") or {}).get("parameters")
		if not parameters:
			return None
		total_bytes = sum(
			count * DTYPE_BYTES.get(dtype, _UNKNOWN_DTYPE_BYTES)
			for dtype, count in parameters.items()
		)
		return round(total_bytes / 1_000_000_000, 2)

	def _hf_json(self, url_template):
		"""Fetch JSON from Hugging Face. Sends the site's HF token when there is one — gated
		repos 401 without it, as do repos that don't exist (HF doesn't distinguish)."""
		token = frappe.conf.get("hf_token")
		headers = {"Authorization": f"Bearer {token}"} if token else {}
		try:
			response = requests.get(
				url_template.format(repo=self.hf_repo), headers=headers, timeout=30
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
