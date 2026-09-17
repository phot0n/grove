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


# The knobs a replica may override. Blank or 0 means inherit: none of these has 0 as a legal
# value, which is what lets one column carry both "unset" and a real number.
OVERRIDABLE = (
	"dtype",
	"kv_cache_dtype",
	"gpu_memory_utilization",
	"max_num_batched_tokens",
	"max_num_seqs",
	"attention_backend",
	"max_model_len",
)
# Taken off the deployment alone: these decide the shape the replicas share and what the compile
# cache is keyed on. A Check cannot express "inherit", which is why allow_long_max_model_len is
# here too.
DEPLOYMENT_ONLY = (
	"pipeline_parallel_size",
	"allow_long_max_model_len",
	"startup_command",
)
# Appended, not replaced: a replica's flags land AFTER the deployment's and vLLM takes the last
# occurrence of a repeated flag, so a replica can override one without retyping the rest.
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
		"""`<model id>-<n>` (`grove/naming.py`), e.g. `qwen3-35b-00001`. Numbered because several
		deployments of one model are normal.

		The shape stays out of the name: `gpus_per_replica` is editable and a name is not, so
		`4xh100` would go stale the first time someone re-shaped this."""
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
		"""Frozen once a replica is past Draft: swapping it while an old container holds its port
		would have the new one fail to bind, and no status returns to Draft. A new deployment is
		how a service moves images, with its replicas up beside the old ones."""
		if self.is_new() or not self.has_value_changed("engine_image"):
			return
		if placed := [r for r in self.replicas if r.status != "Draft"]:
			frappe.throw(
				f"Engine Image cannot change while {self.name} has placed replicas "
				f"({', '.join(r.name for r in placed)}). Create a new Model Deployment to "
				"serve from a different image."
			)

	def resolved_config(self, replica=None):
		"""This deployment's values, with the replica's own wherever it set one. The sole owner of
		the inherit rule: the replica's engine and the deployment's preview both come through
		here, so the two cannot be computed differently."""
		config = {key: self.get(key) for key in DEPLOYMENT_ONLY}
		for key in OVERRIDABLE:
			config[key] = (replica.get(key) if replica else None) or self.get(key)
		for key in ADDITIVE:
			config[key] = " ".join(
				part for part in (self.get(key), replica.get(key) if replica else None) if part
			)
		return config

	def engine_for(self, replica=None, gpu_vram_gb=None, compute_capability=None):
		"""The Engine a replica runs — the one place either doc builds one.

		`replica=None` is the deployment's own preview, off its declared shape rather than a box's.
		`gpu_vram_gb` and `compute_capability` are what the scheduler hands in: the cards a
		candidate box would really give it, which is stricter than the declared minimums."""
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
		"""Additive, not replacing — the same layering `_engine_env` does over Grove's own vars."""
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
		"""`(inference_server, [gpu, ...])` for one more replica, cards named. The policy orders
		viable boxes and cannot make an invalid one viable.

		With none, throws naming why EVERY box was rejected: a scheduler that says only "no
		capacity" is the infuriating kind."""
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
		"""Every box that can take a replica, best first. The whole list rather than the winner,
		because losing a race for a card is not a failure — `add_replica` walks down."""
		scorers = placement_policy(self.placement_policy or "balanced")
		viable = [c for c in self._candidates() if c.is_viable]
		return sorted(viable, key=lambda c: sort_key(c, scorers))

	def _candidates(self):
		"""Every Active, provisioned box measured against this deployment's shape. Rejected boxes
		are kept, carrying their reason: that is the whole error message when nothing fits."""
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
			# A model streamed from S3 is fetched the same way everywhere, so no box is warmer.
			has_local_weights=box.name in siblings and not self.streams_weights,
			active_replicas=claim.replicas,
			replicas_in_region=per_region.get(box.region or "", 0),
			rejection=self._rejection(claim, free, box_architecture, image_architecture),
		)

	def _rejection(self, claim, free, box_architecture, image_architecture):
		"""Why this box cannot take a replica, or "" if it can."""
		# No architecture recorded means on-prem: nothing to check against.
		if box_architecture and image_architecture and box_architecture != image_architecture:
			return f"runs {box_architecture}, and {self.engine_image} is {image_architecture}"
		# An unpinned replica claims no cards but uses them, so every card here READS free while
		# some are busy. Placing would double-book VRAM.
		if claim.unpinned:
			return f"{claim.unpinned} replica(s) here pin no cards, so what is free cannot be known"
		if len(free) < self.gpus_per_replica:
			return (
				f"{len(free)} free card(s) match this shape, and it needs {self.gpus_per_replica}"
				f"{f' of {self.gpu_type}' if self.gpu_type else ''}"
			)
		# The engine's own arithmetic against the cards this box would really give it — stricter
		# than the declared min_vram_gb, and it catches weights that do not fit before a play.
		taking = free[: self.gpus_per_replica]
		vram = min(claim.vram_by_card[card] for card in taking)
		# The weakest card decides both: a mixed box runs at its smallest VRAM and its oldest
		# silicon. 0 from an unscanned card is unknown, and an unknown skips the check.
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
		"""From any deployment of the Model: the HF cache is keyed on the repo, not on who asked
		for it."""
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
		"""Button: place one more replica on a box and serve it. Creates the Model Replica and
		calls its own `setup()`, so this adds no second deploy path.

		`gpus` is CUDA indices, as a list or a comma-separated string — what an operator reads off
		nvidia-smi, typed here and resolved here and nowhere else. Everything past this addresses
		the card itself. None means single-GPU unpinned.

		With no box named the scheduler picks one, and moves down its ranking if a sibling takes
		those cards first."""
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

		The lease goes first, and is what keeps a rival from BLOCKING: a claim is invisible until
		it commits and its row is locked meanwhile, so a rival would wait out this whole
		transaction. A lease is visible the moment it is written.

		One savepoint, because the claim is what can fail — without it a lost race leaves a replica
		holding nothing, which the next scan counts as an unpinned box and refuses to place on."""
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
		# Published before anything slow: until this commits a rival blocks on the locked row for
		# however long the rest of the request takes, and setup() enqueues a play.
		frappe.db.commit()
		replica.setup()
		return replica.name


def _claims_by_box(boxes):
	"""What each box is running and which of its cards nothing holds. Cards and holders arrive
	together — `held_by` is a column on the card — so this and the allocation panel cannot
	disagree about what is free."""
	machines = sorted({box.machine for box in boxes if box.machine})
	cards = cards_on(machines)
	# A replica pinning no cards holds none, so it cannot be counted from the cards — and that is
	# exactly the case that makes a box look emptier than it is.
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
	"""The cards at these CUDA indices, or throw naming the ones the box does not have. The one
	translation from what an operator typed to what everything else addresses, caught here rather
	than deeper in a placement that has already taken a lease."""
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
	"""CUDA indices as ints, however the caller passed them: a JSON string or a comma-separated
	one from the client, a plain list from Python."""
	if isinstance(gpus, str):
		gpus = json.loads(gpus) if gpus.strip().startswith("[") else gpus.split(",")
	return [int(str(index).strip()) for index in (gpus or []) if str(index).strip() != ""]
