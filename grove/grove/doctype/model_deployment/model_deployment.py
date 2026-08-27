# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

from grove.grove.doctype.engine_image.engine_image import engine_tuning
from grove.grove.doctype.model.model import launch_config
from grove.naming import next_deployment_name
from grove.serving.base import DEFAULT_PORT, build_engine
from grove.utils import slugify


# The knobs a replica may override. Blank or 0 on a replica means inherit: none of these has 0 as
# a legal value, which is what lets one column carry both "unset" and a real number — the same
# reading `engine_port = 0` already has.
OVERRIDABLE = (
	"kv_cache_dtype",
	"gpu_memory_utilization",
	"max_num_batched_tokens",
	"max_num_seqs",
	"attention_backend",
	"max_model_len",
)
# The rest of what an engine is built from, taken off the deployment alone. These decide the shape
# the replicas share and what the compile cache is keyed on. A Check cannot express "inherit"
# either, which is the second reason allow_long_max_model_len is here.
DEPLOYMENT_ONLY = (
	"pipeline_parallel_size",
	"allow_long_max_model_len",
	"startup_command",
)
# Appended rather than replaced: a replica's flags land AFTER the deployment's, and vLLM takes the
# last occurrence of a repeated flag. So a replica can add a flag, or override one of the
# deployment's, without retyping the rest — which matters when the deployment's is a --speculative-config
# blob. Same layering as the env rows, for the same reason.
ADDITIVE = ("extra_serve_args",)


class ModelDeployment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.pod_env.pod_env import PodEnv

		allow_long_max_model_len: DF.Check
		attention_backend: DF.Literal["auto", "FLASH_ATTN", "XFORMERS", "FLASHINFER"]
		engine_image: DF.Link
		engine_kind: DF.Data | None
		env: DF.Table[PodEnv]
		extra_serve_args: DF.SmallText | None
		gpu_memory_utilization: DF.Float
		gpu_model: DF.Data | None
		gpus_per_replica: DF.Int
		health_path: DF.Data | None
		kv_cache_dtype: DF.Literal["auto", "fp8"]
		max_model_len: DF.Data | None
		max_num_batched_tokens: DF.Int
		max_num_seqs: DF.Int
		min_vram_gb: DF.Float
		model: DF.Link
		pipeline_parallel_size: DF.Int
		serve_command: DF.Code | None
		startup_command: DF.SmallText | None
		tensor_parallel_size: DF.Int
	# end: auto-generated types

	def autoname(self):
		"""`<model id>-<n>` (`grove/naming.py`), e.g. `qwen3-35b-00001`.

		Numbered because several deployments of one model are normal — a second shape, or a
		rollout running the old one and the new one side by side. The shape itself stays out of
		the name: `gpus_per_replica` is editable and a name is not, so `4xh100` would go stale the
		first time someone re-shaped this. The list view carries the shape, where it stays true."""
		self.name = next_deployment_name()

	def validate(self):
		self._validate_engine_image()
		engine = self.engine_for()
		if errors := engine.placement_errors:
			frappe.throw("<br>".join(errors))
		# Store what actually reaches --max-model-len; the suffix is input sugar. Blank stays
		# blank — that is how a deployment asks for the engine default.
		if self.max_model_len:
			self.max_model_len = str(engine.max_model_len)
		self.tensor_parallel_size = engine.tensor_parallel_size
		self.serve_command = engine.command

	def _validate_engine_image(self):
		"""The image is frozen once this deployment has a replica past Draft: swapping it while an
		old container still holds its port would have the new one fail to bind, and no status
		returns to Draft. A new deployment is how a service moves to a different image — and its
		replicas can be brought up beside the old ones before those come down."""
		if self.is_new() or not self.has_value_changed("engine_image"):
			return
		if placed := [r for r in self.replicas if r.status != "Draft"]:
			frappe.throw(
				f"Engine Image cannot change while {self.name} has placed replicas "
				f"({', '.join(r.name for r in placed)}). Create a new Model Deployment to "
				"serve from a different image."
			)

	def resolved_config(self, replica=None):
		"""The engine tuning for one replica of this deployment: this deployment's values, with the
		replica's own wherever it set one.

		The sole owner of the inherit rule — `ModelReplica.engine` and this deployment's own
		preview both come through here, so what a replica runs and what the deployment shows can
		never be computed two different ways."""
		config = {key: self.get(key) for key in DEPLOYMENT_ONLY}
		for key in OVERRIDABLE:
			config[key] = (replica.get(key) if replica else None) or self.get(key)
		for key in ADDITIVE:
			config[key] = " ".join(
				part for part in (self.get(key), replica.get(key) if replica else None) if part
			)
		return config

	def engine_for(self, replica=None):
		"""The Engine a replica of this deployment runs — the one place either doc builds one.

		`replica=None` is the deployment's own preview, off its declared shape rather than a box's:
		gpus_per_replica for the GPU count and min_vram_gb for the fit check, which is exactly
		what the scheduler will hand a placement later."""
		kind, image_tuning = engine_tuning(self.engine_image)
		return build_engine(
			kind,
			self.model,
			launch_config(self.model),
			port=(replica.engine_port if replica else 0) or DEFAULT_PORT,
			gpu_count=len(replica.gpus or []) if replica else self.gpus_per_replica,
			gpu_vram_gb=(replica.gpu_vram_gb if replica else None) or self.min_vram_gb,
			**self.resolved_config(replica),
			**image_tuning,
		)

	def engine_env_rows(self, replica=None):
		"""This deployment's env rows, then the replica's on top — additive, not replacing, which
		is the same layering `_engine_env` already does over the vars Grove derives."""
		return [*(self.env or []), *((replica.env if replica else None) or [])]

	@property
	def replicas(self):
		"""The Model Replicas placed from this deployment."""
		return frappe.get_all(
			"Model Replica",
			filters={"model_deployment": self.name},
			fields=["name", "inference_server", "status"],
			order_by="creation",
		)

	@frappe.whitelist()
	def add_replica(self, inference_server, gpus=None):
		"""Button: place one more replica of this deployment on a box and serve it.

		Creates the Model Replica and calls its own `setup()` — this adds no second deploy
		path. `gpus` is the CUDA indices to pin, as a list or a comma-separated string; none
		means single-GPU unpinned, which is what a deployment naming no GPU rows already is."""
		replica = frappe.get_doc(
			{
				"doctype": "Model Replica",
				"model_deployment": self.name,
				"inference_server": inference_server,
				"gpus": [{"gpu_index": index} for index in gpu_indexes(gpus)],
			}
		).insert()
		replica.setup()
		return replica.name


def gpu_indexes(gpus):
	"""CUDA indices as ints, however the caller passed them — a whitelisted method is handed a
	JSON string or a comma-separated one from the client, and a plain list from Python."""
	if isinstance(gpus, str):
		gpus = json.loads(gpus) if gpus.strip().startswith("[") else gpus.split(",")
	return [int(str(index).strip()) for index in (gpus or []) if str(index).strip() != ""]
