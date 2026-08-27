# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import requests

import frappe
from frappe.model.document import Document

from grove import failure
from grove.utils import slugify

# The provider our own engines serve under, and the one a Model made before providers existed is
# read as. Shipped as a fixture, so it exists before the first Model is inserted.
DEFAULT_PROVIDER = "frappe"

HF_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"
# Repo root listing, with a size per file. The limit is well past any real shard count.
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
		provider_base_url: DF.Data | None
		published: DF.Check
		reasoning_parser: DF.Data | None
		thinking: DF.Check
		tool_call_parser: DF.Data | None
		upstream_model_id: DF.Data | None
		weights_gb: DF.Float
		weights_s3_uri: DF.Data | None
	# end: auto-generated types

	def validate(self):
		# mandatory_depends_on on the field is client-side only, so this is the gate that holds
		# for an insert over the API.
		if not self.hf_repo and not self.vendor_base_url:
			frappe.throw(
				f"{self.model_id} needs an HF Repo: nothing else says where its weights come from, "
				"and its provider serves nothing of its own.",
				frappe.MandatoryError,
			)

		self.validate_weights_source()

	def validate_weights_source(self):
		"""The streamer reads safetensors out of a bucket — a GGUF ref names a single file it
		cannot stream."""
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
		"""Name = `<provider>/<model id>`. The name is what clients send as `model` and what routes
		are keyed by, so the id is normalised here and then frozen — `set_only_once` on the field
		is what stops an edit renaming a live model out from under its callers.

		Always prefixed, blank provider included: the prefix IS the id, and one model reachable
		without it would be an id nobody could tell was ours."""
		self.model_id = slugify(self.model_id)
		if not self.model_id:
			frappe.throw("No Model ID set")
		# slugify keeps a slash, and the slash is what separates the provider from the id — an id
		# carrying one would name `frappe/a/b` and read as a provider nobody registered.
		if "/" in self.model_id:
			frappe.throw("Model ID cannot contain '/'")
		self.name = f"{self.provider or DEFAULT_PROVIDER}/{self.model_id}"

		# `published` means "reachable" — it tracks whether a live route exists, and is
		# never a manual claim. It is not an access gate: access is granted per user, via
		# Grove User Group or Grove User.
		if self.published and not is_reachable(self.name, provider=self.provider):
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

	@property
	def vendor_base_url(self):
		"""Where a third party serves this model, or "" when we serve it ourselves. Read off the
		provider rather than stored, so the two can never disagree."""
		return vendor_base_url(self.name, self.provider)

	def reject_if_vendor_served(self, what):
		"""Refuse a self-hosting operation on a model we do not host. The form hides these, but a
		whitelisted method is reachable without the button, and the errors underneath are about a
		missing repo or a missing deployment — true, and no help at all."""
		if self.vendor_base_url:
			frappe.throw(f"{self.name} is served by {self.provider}. {what}, and there is none.")

	@frappe.whitelist()
	def fetch_architecture(self):
		"""Button: read the head and layer counts off the repo's config.json, so the
		parallelism checks have real numbers instead of hand-typed ones."""
		self.reject_if_vendor_served("Architecture is read off an HF repo")
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

	@frappe.whitelist()
	def mirror_weights(self):
		"""Button: copy the safetensors off a box that serves this model into the weights
		bucket, then set Weights S3 URI so the next deploy streams them from S3."""
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
		"""Size of the weights in GB — decimal, to match how GPU VRAM is quoted. What the repo's
		weights actually weigh: its top-level safetensors shards, added up as they are on disk —
		or the single GGUF file, for a repo ref that names a quantization. None when it
		publishes neither.

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


def mirror_server(model):
	"""The Inference Server of an Active deployment of this model — a box that already has
	the weights cached (or can pull them onto the disk that was sized for them)."""
	rows = frappe.get_all(
		"Model Replica",
		filters={"model": model, "status": "Active"},
		fields=["inference_server"],
		limit=1,
	)
	return rows[0].inference_server if rows else None


@failure.reports_failure(doctype="Model")
def mirror_weights_to_s3(model):
	"""Worker: run mirror_weights.yml on the box (hf cache → aws s3 sync), then stamp
	weights_s3_uri on success so new deploys pick the mirror up."""
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
	"""True if a request for `model` has somewhere to go: >=1 Active Model Replica (on-prem), a
	Running standalone Pod (cloud), or a third-party provider we hold an endpoint and a key for.
	`exclude` drops one deployment name (used from Model Replica.on_trash, where the row still
	exists in the DB during delete). `provider` is for a caller mid-insert — see vendor_base_url."""
	filters = {"model": model, "status": "Active"}
	if exclude:
		filters["name"] = ("!=", exclude)
	if frappe.db.get_all("Model Replica", filters=filters, limit=1):
		return True
	if frappe.db.get_all("Pod", filters={"model": model, "status": "Running"}, limit=1):
		return True
	# There is no engine to stand up, so nothing else would ever flip it published: a vendor model
	# is reachable from the moment it exists. The key is not checked here because a provider cannot
	# hold an address without one — validate refuses that. Whether a route is actually pushed is
	# pathway_sync's call, and it does check.
	# ponytail: clearing a provider's Base URL leaves its models published until something touches
	# them. They emit no route, so they 404 rather than mis-route; re-sync on provider change if
	# that ever shows up in practice.
	return bool(vendor_base_url(model, provider))


def vendor_base_url(model, provider=None):
	"""Where a third party serves `model`, or "" when we serve it ourselves.

	`provider` is passed by a doc that is still being inserted: its own row is not in the database
	yet, so reading the link back off the name would find nothing and call a vendor model dark."""
	provider = provider or frappe.db.get_value("Model", model, "provider")
	if not provider:
		return ""
	return frappe.get_cached_doc("Model Provider", provider).base_url or ""


# The fields an engine derives its launch arguments from — read live off the Model, never mirrored
# onto a placement, so editing one here reaches every placement on the next deploy.
LAUNCH_FIELDS = (
	"hf_repo", "weights_s3_uri", "modality", "enable_prefix_caching",
	"enable_auto_tool_choice", "tool_call_parser", "thinking", "reasoning_parser",
	"attention_heads", "weights_gb",
)


def launch_config(model):
	"""This Model's intrinsic launch config as a plain mapping, {} when it names no Model.

	Lives here rather than in grove/serving so that package stays frappe-free: these are Model
	columns, and an engine is handed the answer rather than fetching it."""
	if not model:
		return {}
	return frappe.db.get_value("Model", model, LAUNCH_FIELDS, as_dict=True) or {}


def sync_published(model, exclude=None):
	"""Recompute Model.published to reflect whether it has a live deployment.
	Called whenever a deployment's status changes (deploy / teardown / broken),
	since that's what makes the model reachable. Written via db.set_value so it
	skips validate (no recursion) and is cheap."""
	if not model or not frappe.db.exists("Model", model):
		return
	want = 1 if is_reachable(model, exclude=exclude) else 0
	if frappe.db.get_value("Model", model, "published") != want:
		frappe.db.set_value("Model", model, "published", want)
