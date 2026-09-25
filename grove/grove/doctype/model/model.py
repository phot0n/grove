# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import requests

import frappe
from frappe.model.document import Document

from grove import failure
from grove.grove.doctype.model_provider.model_provider import self_hosted_provider
from grove.utils import slugify

HF_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"
# Root listing with a size per file. The limit is well past any real shard count.
HF_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/main?limit=1000"


class Model(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attention_heads: DF.Int
		enable_auto_tool_choice: DF.Check
		enable_prefix_caching: DF.Check
		hf_repo: DF.Data | None
		hidden_layers: DF.Int
		modality: DF.Literal["text", "multimodal", "embedding", "audio"]
		model_id: DF.Data
		provider: DF.Link | None
		provider_is_self_hosted: DF.Check
		published: DF.Check
		reasoning_parser: DF.Data | None
		thinking: DF.Check
		tool_call_parser: DF.Data | None
		torch_dtype: DF.Data | None
		upstream_model_id: DF.Data | None
		weights_gb: DF.Float
		weights_s3_uri: DF.Data | None
	# end: auto-generated types

	def validate(self):
		self.provider_is_self_hosted = self.is_self_hosted
		# mandatory_depends_on is client-side only; this is the gate an API insert hits.
		if self.is_self_hosted and not self.hf_repo:
			frappe.throw(
				f"{self.model_id} needs an HF Repo: nothing else says where its weights come from, "
				"and its provider serves nothing of its own.",
				frappe.MandatoryError,
			)

		self.validate_weights_source()

	def validate_weights_source(self):
		"""The streamer reads safetensors out of a bucket; a GGUF ref names one file it cannot
		stream."""
		if not self.weights_s3_uri:
			return
		if not self.weights_s3_uri.startswith("s3://"):
			frappe.throw("Weights S3 URI must start with s3://")
		if self.gguf_quant:
			frappe.throw(
				"A GGUF ref cannot stream — the runai streamer needs safetensors. Clear "
				"Weights S3 URI, or point HF Repo at the safetensors repo."
			)

	def autoname(self):
		"""Name = `<provider>/<model id>`. What clients send as `model` and what routes are keyed
		by, so the id is normalised here and then frozen by `set_only_once` — an edit would rename
		a live model out from under its callers.

		Always prefixed, blank provider included: the prefix IS the id."""
		self.model_id = slugify(self.model_id)
		if not self.model_id:
			frappe.throw("No Model ID set")
		# slugify keeps a slash, and the slash separates provider from id — one here would name
		# `<provider>/a/b` and read as a provider nobody registered.
		if "/" in self.model_id:
			frappe.throw("Model ID cannot contain '/'")
		self.provider = self.provider or self_hosted_provider()
		if not self.provider:
			frappe.throw(
				"No Model Provider is marked Self Hosted, so a blank provider has nothing to default to."
			)
		self.name = f"{self.provider}/{self.model_id}"

		# `published` means "reachable", never a manual claim, and is not an access gate —
		# access is granted per user via Model Group or Grove User.
		if self.published and not is_reachable(self.name, provider=self.provider):
			self.published = 0

	@property
	def repo_id(self):
		"""The repo alone. A GGUF repo publishes a dozen quantizations, so vLLM is pointed at one
		with `unsloth/Qwen3-0.6B-GGUF:Q4_K_M` — a ref the HF API does not take."""
		return (self.hf_repo or "").split(":")[0]

	@property
	def gguf_quant(self):
		"""The quantization named after the colon, blank for a safetensors repo."""
		return (self.hf_repo or "").partition(":")[2]

	@property
	def is_self_hosted(self):
		"""Our own engines serve it — the provider is the flagged one. Read off the provider rather
		than the mirror, so the two cannot disagree."""
		return bool(
			self.provider and frappe.db.get_value("Model Provider", self.provider, "is_self_hosted")
		)

	def reject_if_vendor_served(self, what):
		"""Refuse a self-hosting operation on a model we do not host. The form hides these, but a
		whitelisted method is reachable without the button — and the errors underneath name a
		missing repo, which is true and no help at all."""
		if not self.is_self_hosted:
			frappe.throw(f"{self.name} is served by {self.provider}. {what}, and there is none.")

	@frappe.whitelist()
	def fetch_architecture(self):
		"""Button: read the shape off the repo's config.json, so the parallelism checks have real
		numbers instead of hand-typed ones."""
		self.reject_if_vendor_served("Architecture is read off an HF repo")
		if not self.hf_repo:
			frappe.throw("Set the HF Repo first — that's what's read.")
		config = self.get_hf_config()
		# Multimodal repos nest the language model's shape; the top level describes the whole
		# thing, vision tower included.
		shape = config.get("text_config") or config.get("llm_config") or {}
		heads = shape.get("num_attention_heads") or config.get("num_attention_heads")
		layers = shape.get("num_hidden_layers") or config.get("num_hidden_layers")
		if not (heads and layers):
			frappe.throw(f"{self.hf_repo}'s config.json has no head/layer count to read.")
		values = {"attention_heads": heads, "hidden_layers": layers}
		if dtype := config_dtype(config, shape):
			values["torch_dtype"] = dtype
		if weights_gb := self.get_weights_gb():
			values["weights_gb"] = weights_gb
		self.db_set(values)
		frappe.msgprint(
			f"{self.hf_repo}: {heads} attention heads, {layers} layers"
			+ (f", {values['weights_gb']} GB of weights." if "weights_gb" in values else ".")
		)
		return values

	@frappe.whitelist()
	def mirror_weights(self):
		"""Button: copy the safetensors off a box that serves this model into the weights bucket,
		then set Weights S3 URI so the next deploy streams them."""
		self.reject_if_vendor_served("Weights are mirrored off a box that serves the model")
		settings = frappe.get_single("Grove Settings")
		if not settings.weights_s3_write_environment:
			frappe.throw("Set Weights Bucket and the Mirror keys in Grove Settings first.")
		if self.gguf_quant:
			frappe.throw("A GGUF ref cannot be mirrored — the streamer needs safetensors.")
		if not mirror_server(self.name):
			frappe.throw(
				f"No Active Model Replica serves {self.name}. The mirror runs from a box "
				"that has the weights cached — deploy the model once first."
			)
		frappe.enqueue(
			"grove.grove.doctype.model.model.mirror_weights_to_s3",
			model=self.name,
			queue="long",
			timeout=28800,
		)
		frappe.msgprint(
			f"Mirroring {self.hf_repo} to {settings.weights_bucket} — watch the box's Ansible Plays.",
			alert=True,
		)

	def get_hf_config(self):
		"""The repo's config.json — its architecture."""
		return self._hf_json(HF_CONFIG_URL)

	def get_weights_gb(self):
		"""Size of the weights in GB — decimal, to match how GPU VRAM is quoted. The top-level
		safetensors shards as they are on disk, or the single GGUF file.

		Measured, not costed out from parameter counts: a packed quantization reports its
		CONTAINER type, so GLM-5.2-AWQ-INT4 comes back as 726 billion I32 and prices at 2959 GB
		against a real 474. Subfolders are skipped because that is where a repo keeps other
		quantizations of the same weights."""
		suffix = f"{self.gguf_quant}.gguf" if self.gguf_quant else ".safetensors"
		total_bytes = sum(
			entry.get("size") or 0
			for entry in self._hf_json(HF_TREE_URL)
			if entry.get("type") == "file" and is_root_weights_file(entry.get("path", ""), suffix)
		)
		return round(total_bytes / 1_000_000_000, 2) or None

	def _hf_json(self, url_template):
		"""Sends the site's HF token when there is one: gated repos 401 without it, as do repos
		that don't exist — HF doesn't distinguish."""
		token = frappe.conf.get("hf_token")
		headers = {"Authorization": f"Bearer {token}"} if token else {}
		response = requests.get(url_template.format(repo=self.repo_id), headers=headers, timeout=30)
		if response.status_code in (401, 403, 404):
			frappe.throw(
				f"Hugging Face returned {response.status_code} for {self.hf_repo} — the repo is "
				"gated, private or misspelled. Gated repos need hf_token in the site config."
			)
		if not response.ok:
			frappe.throw(f"Hugging Face returned {response.status_code} for {self.hf_repo}.")
		return response.json()


def is_root_weights_file(path, suffix):
	"""At the root, not in a subfolder. The listing is already non-recursive, so this is a second
	line of defence: counting a subfolder's quantizations bills the same model twice."""
	return path.endswith(suffix) and "/" not in path


def mirror_server(model):
	"""A box that already has the weights cached, or can pull them onto the disk sized for
	them."""
	rows = frappe.get_all(
		"Model Replica",
		filters={"model": model, "status": "Active"},
		fields=["inference_server"],
		limit=1,
	)
	return rows[0].inference_server if rows else None


@failure.reports_failure(doctype="Model")
def mirror_weights_to_s3(model):
	"""Worker: run mirror_weights.yml on the box, then stamp weights_s3_uri on success so new
	deploys pick the mirror up."""
	doc = frappe.get_doc("Model", model)
	settings = frappe.get_single("Grove Settings")
	inf = frappe.get_doc("Inference Server", mirror_server(model))
	uri = f"{settings.weights_bucket}/models/{doc.repo_id.replace('/', '--')}"
	play_name, rc = inf.run_playbook(
		"mirror_weights.yml",
		extravars={
			"vllm_home": inf.data_path,
			"vllm_hf_home": inf.hf_home,
			"vllm_hf_token": frappe.conf.get("hf_token", ""),
			"mirror_repo": doc.repo_id,
			"mirror_uri": uri,
			"mirror_env": settings.weights_s3_write_environment,
		},
		reference_doctype="Model",
		reference_docname=model,
	)
	if rc == 0:
		doc.db_set("weights_s3_uri", uri)
	return play_name, rc


def is_reachable(model, exclude=None, provider=None):
	"""True if a request for `model` has somewhere to go: an Active Model Replica, a Running Pod,
	or a third-party provider we hold an endpoint and key for.

	`exclude` drops one name, for on_trash where the row still exists during delete. `provider` is
	for a caller mid-insert — see vendor_base_url."""
	filters = {"model": model, "status": "Active"}
	if exclude:
		filters["name"] = ("!=", exclude)
	if frappe.db.get_all("Model Replica", filters=filters, limit=1):
		return True
	if frappe.db.get_all("Pod", filters={"model": model, "status": "Running"}, limit=1):
		return True
	# A vendor model is reachable from the moment it exists — nothing else would ever flip it
	# published. The key is unchecked because validate refuses a provider holding an address
	# without one.
	# TODO: clearing a provider's Base URL leaves its models published until something
	# touches them. They emit no route, so they 404 rather than mis-route.
	return bool(vendor_base_url(model, provider))


def vendor_base_url(model, provider=None):
	"""Where a third party serves `model`, "" when we serve it. `provider` is passed by a doc still
	being inserted: its row is not in the database yet, so reading the link off the name would find
	nothing and call a vendor model dark."""
	provider = provider or frappe.db.get_value("Model", model, "provider")
	if not provider:
		return ""
	provider = frappe.get_cached_doc("Model Provider", provider)
	return provider.base_url or provider.anthropic_base_url or ""


# Read live off the Model, never mirrored onto a placement, so editing one reaches every placement
# on the next deploy.
LAUNCH_FIELDS = (
	"hf_repo", "weights_s3_uri", "modality", "enable_prefix_caching",
	"enable_auto_tool_choice", "tool_call_parser", "thinking", "reasoning_parser",
	"attention_heads", "weights_gb", "torch_dtype",
)


def config_dtype(config, shape=None):
	"""What vLLM will serve these weights in when nothing overrides it.

	Two spellings and two places: transformers renamed `torch_dtype` to `dtype` in 4.57, and a
	multimodal repo states it on the LANGUAGE model — Qwen3.5-4B has `text_config.dtype` and
	nothing above it. Missing it reads as "unknown" and silently skips the capability check."""
	for source in (shape or {}, config):
		if dtype := (source.get("torch_dtype") or source.get("dtype") or ""):
			return dtype
	return ""


def launch_config(model):
	"""This Model's intrinsic launch config as a plain mapping. Lives here rather than in
	grove/serving so that package stays frappe-free."""
	if not model:
		return {}
	return frappe.db.get_value("Model", model, LAUNCH_FIELDS, as_dict=True) or {}


def sync_published(model, exclude=None):
	"""Recompute Model.published, called after every deployment status change. Written via
	db.set_value so it skips validate — no recursion."""
	if not model or not frappe.db.exists("Model", model):
		return

	want = 1 if is_reachable(model, exclude=exclude) else 0
	frappe.db.set_value("Model", model, "published", want)
