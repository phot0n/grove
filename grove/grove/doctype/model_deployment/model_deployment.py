# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

from grove.grove.doctype.engine_image.engine_image import engine_tuning
from grove.grove.doctype.gpu.gpu import GPUUnavailable, cards_on
from grove.grove.doctype.model.model import launch_config
from grove.grove.doctype.model_replica.model_replica import GPU_CLAIMING_STATUSES
from grove.naming import next_deployment_name
from grove.placement import lease
from grove.placement.base import Candidate, fitting_gpus, placement_policy, sort_key
from grove.serving.base import DEFAULT_PORT, build_engine


# The knobs a replica may override. Blank or 0 on a replica means inherit: none of these has 0 as
# a legal value, which is what lets one column carry both "unset" and a real number — the same
# reading `engine_port = 0` already has.
OVERRIDABLE = (
	"dtype",
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
		gpu_type: DF.Link | None
		gpus_per_replica: DF.Int
		health_path: DF.Data | None
		dtype: DF.Literal["auto", "bfloat16", "float16"]
		kv_cache_dtype: DF.Literal["auto", "fp8"]
		max_model_len: DF.Data | None
		max_num_batched_tokens: DF.Int
		max_num_seqs: DF.Int
		min_vram_gb: DF.Float
		model: DF.Link
		pipeline_parallel_size: DF.Int
		placement_policy: DF.Literal["balanced", "pack", "spread"]
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

	def engine_for(self, replica=None, gpu_vram_gb=None, compute_capability=None):
		"""The Engine a replica of this deployment runs — the one place either doc builds one.

		`replica=None` is the deployment's own preview, off its declared shape rather than a box's:
		gpus_per_replica for the GPU count and min_vram_gb for the fit check.

		`gpu_vram_gb` is what the scheduler hands in — the card size a candidate box would really
		give it, which is stricter than the declared min_vram_gb and is the point of asking.
		`compute_capability` is the same idea for what those cards can DO: a box can have room for
		the weights and still be unable to represent them."""
		kind, image_tuning = engine_tuning(self.engine_image)
		return build_engine(
			kind,
			self.model,
			launch_config(self.model),
			port=(replica.engine_port if replica else 0) or DEFAULT_PORT,
			gpu_count=len(replica.gpus or []) if replica else self.gpus_per_replica,
			gpu_vram_gb=gpu_vram_gb or (replica.gpu_vram_gb if replica else None) or self.min_vram_gb,
		compute_capability=(
			compute_capability or (replica.gpu_compute_capability if replica else None) or 0
		),
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
	def find_placement(self):
		"""`(inference_server, [gpu, ...])` for one more replica of this deployment, cards named.

		Chooses only among boxes that can actually take it — the policy orders viable boxes and
		cannot make an invalid one viable. With none, throws naming why EVERY box was rejected:
		a scheduler that says only "no capacity" is the infuriating kind."""
		candidates = self._candidates()
		viable = [c for c in candidates if c.is_viable]
		if not viable:
			frappe.throw(
				"<br>".join(f"<b>{c.inference_server}</b>: {c.rejection}" for c in candidates)
				or "No Inference Server is Active and provisioned."
			)
		best = self.ranked_placements()[0]
		return best.inference_server, list(best.fitting_gpus[: self.gpus_per_replica])

	def ranked_placements(self):
		"""Every box that can take a replica, best first by this deployment's policy.

		The whole list rather than the winner, because losing a race for a card is not a failure:
		the next box is already ranked, and `add_replica` just walks down."""
		scorers = placement_policy(self.placement_policy or "balanced")
		viable = [c for c in self._candidates() if c.is_viable]
		return sorted(viable, key=lambda c: sort_key(c, scorers))

	def _candidates(self):
		"""Every Active, provisioned box measured against this deployment's shape.

		Rejected boxes are kept, carrying their reason, because the reason is the whole error
		message when nothing fits."""
		boxes = frappe.get_all(
			"Inference Server",
			filters={"status": "Active", "is_provisioned": 1},
			fields=["name", "machine", "region"],
		)
		if not boxes:
			return []
		claims = _claims_by_box(boxes)
		architectures = _machine_architectures([box.machine for box in boxes])
		image_architecture = frappe.db.get_value("Engine Image", self.engine_image, "cpu_architecture")
		siblings = self._sibling_boxes()
		per_region = self._replicas_per_region()
		return [
			self._candidate(box, claims[box.name], architectures.get(box.machine), image_architecture,
			                siblings, per_region)
			for box in boxes
		]

	def _candidate(self, box, claim, box_architecture, image_architecture, siblings, per_region):
		free = fitting_gpus(claim.free_gpus, self.gpu_type, self.min_vram_gb)
		# Cards a placement in flight has announced, which no committed row shows yet.
		busy = lease.leased(free)
		free = tuple(card for card in free if card not in busy)
		return Candidate(
			inference_server=box.name,
			region=box.region or "",
			fitting_gpus=free,
			surplus=len(free) - self.gpus_per_replica,
			# Only worth something when the weights actually live on a box. A model streamed from
			# S3 is fetched the same way everywhere, so no box is warmer than another.
			has_local_weights=box.name in siblings and not self.streams_weights,
			active_replicas=claim.replicas,
			replicas_in_region=per_region.get(box.region or "", 0),
			rejection=self._rejection(claim, free, box_architecture, image_architecture),
		)

	def _rejection(self, claim, free, box_architecture, image_architecture):
		"""Why this box cannot take a replica, or "" if it can."""
		# A box with no architecture recorded is on-prem — nothing to check against, the same
		# reading `_validate_engine_architecture` already takes.
		if box_architecture and image_architecture and box_architecture != image_architecture:
			return f"runs {box_architecture}, and {self.engine_image} is {image_architecture}"
		# An unpinned replica claims no cards but uses them, so every card here READS free while
		# some are busy. Declining is the only honest answer — placing would double-book VRAM.
		if claim.unpinned:
			return f"{claim.unpinned} replica(s) here pin no cards, so what is free cannot be known"
		if len(free) < self.gpus_per_replica:
			return (
				f"{len(free)} free card(s) match this shape, and it needs {self.gpus_per_replica}"
				f"{f' of {self.gpu_type}' if self.gpu_type else ''}"
			)
		# The engine's own arithmetic, against the cards this box would actually give it — a
		# stricter check than the deployment's declared min_vram_gb, and it catches weights that
		# do not fit before a play starts.
		taking = free[: self.gpus_per_replica]
		vram = min(claim.vram_by_card[card] for card in taking)
		# The weakest card decides both: a mixed box is capped by its smallest VRAM and by its
		# oldest silicon. 0 from an unscanned card means unknown, and an unknown skips the check.
		capability = min(claim.capability_by_card[card] for card in taking)
		if errors := self.engine_for(
			gpu_vram_gb=vram, compute_capability=capability
		).placement_errors:
			return "; ".join(errors)
		return ""

	@property
	def streams_weights(self):
		"""Whether this deployment's Model is served from S3 rather than a box's HF cache."""
		return bool(launch_config(self.model).get("weights_s3_uri"))

	def _sibling_boxes(self):
		"""Boxes already serving this deployment's Model — from any deployment of it, since the
		HF cache is keyed on the repo and not on who asked for it."""
		return {
			replica.inference_server
			for replica in frappe.get_all(
				"Model Replica",
				filters={"model": self.model, "status": ("in", GPU_CLAIMING_STATUSES)},
				fields=["inference_server"],
			)
		}

	def _replicas_per_region(self):
		counts = {}
		for replica in self.replicas:
			region = frappe.db.get_value("Inference Server", replica.inference_server, "region") or ""
			counts[region] = counts.get(region, 0) + 1
		return counts

	@frappe.whitelist()
	def add_replica(self, inference_server: str | None = None, gpus: str | list | None = None):
		"""Button: place one more replica of this deployment on a box and serve it.

		Creates the Model Replica and calls its own `setup()` — this adds no second deploy
		path. `gpus` is the CUDA indices to pin, as a list or a comma-separated string; none
		means single-GPU unpinned, which is what a deployment naming no GPU rows already is.

		Indices are what an operator reads off nvidia-smi, so this is where they are typed — and
		the ONLY place they are resolved. Everything past here addresses the card itself.

		With no box named, the scheduler picks one — and if a sibling takes those cards first,
		moves to the next box it already ranked rather than failing the request."""
		if inference_server:
			return self._place(inference_server, cards_at(inference_server, gpu_indexes(gpus)))

		candidates = self.ranked_placements()
		if not candidates:
			self.find_placement()  # throws naming why every box was rejected
		for candidate in candidates:
			try:
				return self._place(
					candidate.inference_server,
					list(candidate.fitting_gpus[: self.gpus_per_replica]),
				)
			except GPUUnavailable:
				continue  # a sibling won these cards; the next box is already ranked
		frappe.throw(
			f"Every box that could take a replica of {self.name} lost its cards to another "
			"placement while this one was being made. Try again."
		)

	def _place(self, inference_server, gpus):
		"""Create the replica and take its cards, or neither. `gpus` are GPU docnames.

		The lease goes first and is what keeps a rival from BLOCKING. A claim is invisible until it
		commits, and the row it takes is locked meanwhile — so a rival trying the same card waits
		out this whole transaction rather than failing fast. A lease is visible the moment it is
		written, so the rival skips the card instead of queueing.

		One savepoint, because the claim is what can fail: without it a lost race would leave a
		replica behind holding nothing, which the next scan would count as an unpinned box and
		refuse to place on."""
		machine = frappe.db.get_value("Inference Server", inference_server, "machine")
		if not lease.take(gpus, self.name):
			raise GPUUnavailable(f"A placement already in flight holds a card on {machine}.")
		frappe.db.savepoint("place_replica")
		try:
			replica = frappe.get_doc(
				{
					"doctype": "Model Replica",
					"model_deployment": self.name,
					"inference_server": inference_server,
					"gpus": [{"gpu": gpu} for gpu in gpus],
				}
			).insert()
		except Exception:
			frappe.db.rollback(save_point="place_replica")
			lease.release(gpus)
			raise
		# Published before anything slow. Until this commits, the claim is invisible AND its index
		# entry is locked, so a rival blocks for however long the rest of this request takes —
		# setup() enqueues a play, and that wait was landing on whoever asked next.
		frappe.db.commit()
		replica.setup()
		return replica.name


def _claims_by_box(boxes):
	"""What each box is running and which of its cards nothing holds.

	Cards and holders arrive together — `held_by` is a column on the card — so this and the
	allocation panel read the same rows and cannot disagree about what is free."""
	machines = sorted({box.machine for box in boxes if box.machine})
	cards = cards_on(machines)
	# A replica pinning no cards holds none, so it cannot be counted from the cards — and it is
	# exactly the case that makes a box look emptier than it is. Counted here so `_rejection` can
	# decline the box rather than place onto cards it cannot see.
	replicas = frappe.get_all(
		"Model Replica",
		filters={"inference_server": ("in", [box.name for box in boxes]),
		         "status": ("in", GPU_CLAIMING_STATUSES)},
		fields=["name", "inference_server"],
	)
	holding = {card.held_by for card in cards if card.held_by}

	claims = {}
	for box in boxes:
		mine = [r for r in replicas if r.inference_server == box.name]
		on_machine = [c for c in cards if c.machine == box.machine]
		claims[box.name] = frappe._dict(
			free_gpus=[c for c in on_machine if not c.held_by],
			vram_by_card={c.name: c.vram_gb for c in on_machine},
			capability_by_card={c.name: c.compute_capability for c in on_machine},
			replicas=len(mine),
			unpinned=len([r for r in mine if r.name not in holding]),
		)
	return claims


def _machine_architectures(machine_names):
	"""Each Machine's cpu_architecture. Blank is on-prem and means "do not check"."""
	return {
		machine.name: machine.cpu_architecture
		for machine in frappe.get_all(
			"Machine",
			filters={"name": ("in", sorted({name for name in machine_names if name}))},
			fields=["name", "cpu_architecture"],
		)
	}


def cards_at(inference_server, indexes):
	"""The cards at these CUDA indices on a box, or throw naming the ones it does not have.

	The one translation from what an operator typed to what everything else addresses. A bad index
	is an input error, so it is caught here rather than deeper in a placement that has already
	taken a lease."""
	machine = frappe.db.get_value("Inference Server", inference_server, "machine")
	cards = {int(card.gpu_index): card.name for card in cards_on([machine])}
	missing = [index for index in indexes if index not in cards]
	if missing:
		frappe.throw(
			f"{machine} has no card at CUDA index {', '.join(str(index) for index in missing)}. "
			"Re-scan the box."
		)
	return [cards[index] for index in indexes]


def gpu_indexes(gpus):
	"""CUDA indices as ints, however the caller passed them — a whitelisted method is handed a
	JSON string or a comma-separated one from the client, and a plain list from Python."""
	if isinstance(gpus, str):
		gpus = json.loads(gpus) if gpus.strip().startswith("[") else gpus.split(",")
	return [int(str(index).strip()) for index in (gpus or []) if str(index).strip() != ""]
