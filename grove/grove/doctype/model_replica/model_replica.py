# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import hashlib
import re
import secrets

import frappe
from frappe.model.document import Document

from grove import failure
from grove.grove.doctype.gpu.gpu import (
	GPUUnavailable,
	cards_on,
	claim,
	release,
	release_if_stale,
)
from grove.naming import next_replica_name
from grove.utils import is_env_key, is_env_value


ENGINE_PORT_BASE = 8080
# Only teardown takes the container off the box, so only Terminated releases the port. Inactive
# keeps its: Start expects the same one back.
_PORT_FREE_STATUSES = ("Terminated",)
# Provisioning holds because deploy_model checks the claim BEFORE it flips the status; Broken
# holds because --restart unless-stopped brings a crash-looping engine back onto its cards.
# Inactive does not: a stopped container holds no VRAM, so its cards are offered to siblings.
GPU_CLAIMING_STATUSES = ("Provisioning", "Active", "Broken")
# Draft holds too: a replica takes its cards the moment its row exists, which is what makes two
# concurrent placements impossible rather than merely unlikely. The cost is that an abandoned
# Draft strands its cards until it is deleted.
CLAIM_HOLDING_STATUSES = ("Draft", *GPU_CLAIMING_STATUSES)
KV_CACHE_MEMORY_LINE = re.compile(
	r"Replace gpu_memory_utilization config with `--kv-cache-memory=(\d+)`"
)


class ModelReplica(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.model_replica_gpu.model_replica_gpu import ModelReplicaGPU
		from grove.grove.doctype.pod_env.pod_env import PodEnv

		attention_backend: DF.Literal["", "auto", "FLASH_ATTN", "XFORMERS", "FLASHINFER"]
		engine_port: DF.Int
		engine_url: DF.Data | None
		env: DF.Table[PodEnv]
		extra_serve_args: DF.SmallText | None
		geography: DF.Link | None
		gpu_memory_utilization: DF.Float
		gpus: DF.Table[ModelReplicaGPU]
		inference_server: DF.Link
		internal_api_key: DF.Password | None
		dtype: DF.Literal["", "auto", "bfloat16", "float16"]
		kv_cache_dtype: DF.Literal["", "auto", "fp8"]
		kv_cache_memory: DF.Int
		kv_cache_memory_for: DF.SmallText | None
		log_lines: DF.Int
		max_model_len: DF.Data | None
		max_num_batched_tokens: DF.Int
		max_num_seqs: DF.Int
		model: DF.Link
		model_deployment: DF.Link
		region: DF.Link | None
		serve_command: DF.Code | None
		status: DF.Literal["Draft", "Provisioning", "Active", "Inactive", "Terminated", "Broken"]
	# end: auto-generated types

	# No on_update sync hook: any change here moves the routes snapshot hash and
	# grove.pathway_sync.sync_projection pushes it on the next tick.

	@property
	def deployment(self):
		"""What it serves, on what shape, with which tuning. This doc owns only WHERE: the box,
		the cards, the port."""
		if not self.model_deployment:
			frappe.throw(f"Model Replica {self.name} has no Model Deployment.")
		return frappe.get_cached_doc("Model Deployment", self.model_deployment)

	@property
	def engine(self):
		"""The deployment's shape and tuning with this replica's overrides applied. Built BY the
		deployment, so the preview there and the command that runs here cannot differ.

		The learned KV cache figure is bolted on here, the one judge of whether it still applies:
		validity is keyed on the command WITHOUT it, which is what the deployment just built."""
		engine = self.deployment.engine_for(self)
		if self.kv_cache_memory and self.kv_cache_memory_for == self.kv_cache_memory_key(engine):
			engine.kv_cache_memory = self.kv_cache_memory
		return engine

	def kv_cache_memory_key(self, engine):
		"""Everything the learned figure is only valid for, hashed: image, card VRAM, and the
		command. `engine` is built without the hint, so the flag itself never moves the key."""
		measured_under = f"{self.deployment.engine_image}\n{self.gpu_vram_gb}\n{engine.command}"
		return hashlib.sha256(measured_under.encode()).hexdigest()

	def learned_kv_cache_memory(self):
		"""What a profiled boot just taught us, as columns to write. Best-effort: the replica is
		healthy, and a log read failing is not a reason to call it Broken."""
		engine = self.engine
		if engine.kv_cache_memory:
			return {}  # booted with the hint, so vLLM did not profile and there is nothing new
		try:
			figure = parse_kv_cache_memory(self.get_engine_logs(lines=5000))
		except Exception:
			frappe.log_error(title=f"KV cache figure not read: {self.name}")
			return {}
		if not figure:
			return {}
		return {"kv_cache_memory": figure, "kv_cache_memory_for": self.kv_cache_memory_key(engine)}

	def autoname(self):
		"""`<model id>-<region>-<server>-<n>`, e.g. `qwen3-8b-ap-south-1-inf3-00007`.

		Model and region are read directly because NEITHER field's `fetch_from` has run this
		early — a replica created from a deployment would otherwise name itself with a blank model
		and fail its own mandatory check."""
		self.model = frappe.db.get_value("Model Deployment", self.model_deployment, "model")
		region = frappe.db.get_value("Inference Server", self.inference_server, "region")
		self.name = next_replica_name(self.model, self.inference_server, region)

	def validate(self):
		self._assign_engine_port()
		self._derive_engine_url()
		self._validate_gpus()
		self._validate_kv_cache_memory()
		self._validate_engine()

	def _validate_kv_cache_memory(self):
		"""An operator clearing the figure forgets its key; typing one keys it to today's argv."""
		if not self.kv_cache_memory:
			self.kv_cache_memory_for = ""
		elif self.has_value_changed("kv_cache_memory"):
			self.kv_cache_memory_for = self.kv_cache_memory_key(self.deployment.engine_for(self))

	def _validate_engine(self):
		"""Env rows are rendered into a docker --env-file, which is a trust boundary: a newline in
		a value would otherwise append a variable of the operator's choosing."""
		self._validate_engine_architecture()
		for row in self.env or []:
			if not is_env_key(row.key):
				frappe.throw(f"'{row.key}' is not a valid environment variable name.")
			if not is_env_value(row.value):
				frappe.throw(f"Value for '{row.key}' cannot contain a newline or a double quote.")

	def _validate_engine_architecture(self):
		"""Docker pulls the wrong architecture happily and fails at exec, deep inside a play, with
		nothing naming the cause. A box with no architecture recorded is on-prem — nothing to
		check against."""
		machine = frappe.db.get_value("Inference Server", self.inference_server, "machine")
		box_architecture = frappe.db.get_value("Machine", machine, "cpu_architecture") if machine else None
		if not box_architecture:
			return
		image = self.deployment.engine_image
		image_architecture = frappe.db.get_value("Engine Image", image, "cpu_architecture")
		if image_architecture != box_architecture:
			frappe.throw(
				f"Engine Image {image} is {image_architecture}, but "
				f"{self.inference_server} runs on {box_architecture}. Place this replica on an "
				f"{image_architecture} box, or point it at a deployment with an "
				f"{box_architecture} image."
			)

	@property
	def gpu_vram_gb(self):
		sizes = [row.vram_gb for row in self.gpus or [] if row.vram_gb]
		return min(sizes) if sizes else None

	@property
	def gpu_compute_capability(self):
		"""The weakest pinned card: a mixed box runs at its oldest. None reads as "do not
		check"."""
		levels = [row.compute_capability for row in self.gpus or [] if row.compute_capability]
		return min(levels) if levels else None

	def _validate_gpus(self):
		"""Reject duplicate or off-box cards, and rebuild the serve command preview.

		The display columns are `fetch_from` the linked `GPU`, so they cannot drift. What still
		has to be checked is that the card is on THIS replica's box: the Link field will accept
		one from any machine in the fleet."""
		seen = set()
		for r in self.gpus or []:
			if r.gpu in seen:
				frappe.throw(f"GPU {r.gpu} is listed twice.")
			seen.add(r.gpu)

		# No box yet → the reqd check on inference_server flags it; the shape split is still checked.
		if self.gpus and self.inference_server:
			mine = {card.name for card in cards_on([self.server.machine])}
			for r in self.gpus:
				if r.gpu not in mine:
					frappe.throw(
						f"GPU {r.gpu} is not on {self.inference_server}'s machine. Pick a card "
						"from this box."
					)

		self._validate_shape()

		engine = self.engine
		if errors := engine.placement_errors:
			frappe.throw("<br>".join(errors))
		# Store what actually reaches --max-model-len; the suffix is input sugar. Blank stays
		# blank — that is how a placement asks for the engine default.
		if self.max_model_len:
			self.max_model_len = str(engine.max_model_len)
		# From the same builder the deploy uses, so what is shown is what runs. The only rendered
		# field left here: it carries this replica's port and overrides, so it genuinely differs
		# from the deployment's.
		self.serve_command = engine.command

	def _validate_shape(self):
		"""A replica has to take as many cards as its deployment declares — that is what makes
		replicas interchangeable, so an autoscaler can divide by `replicas x capacity`, and what
		lets the deployment's tensor parallel size be the only one there is.

		Naming no GPUs at all is the unpinned single-GPU case and is left alone."""
		declared = self.deployment.gpus_per_replica
		if self.gpus and declared and len(self.gpus) != declared:
			frappe.throw(
				f"{self.model_deployment} places replicas on {declared} GPU(s), but this one "
				f"names {len(self.gpus)}. Pin {declared} of them, or use a deployment with a "
				"shape that matches this box."
			)

	def sync_gpu_claims(self):
		"""Make the cards this replica holds match what its status says it should — the one place
		the claim rule lives.

		Called after every status transition rather than from `validate`, because status moves by
		`db.set_value`, which never runs validate."""
		if self.status in CLAIM_HOLDING_STATUSES:
			self.claim_gpus()
		else:
			self.release_gpus()

	def claim_gpus(self):
		"""Take each pinned card, or throw naming the one that was lost.

		A compare-and-swap per card: `held_by` moves only if it was empty, so two replicas cannot
		both win one. A card held by a replica no longer entitled to it is cleared first — that is
		where a worker dying between a status flip and its release gets repaired.

		All or nothing, or a replica would sit on cards it is not going to use."""
		taken = []
		for gpu in self.gpu_records:
			if gpu.held_by == self.name:
				continue  # already ours, from an earlier transition
			if gpu.held_by:
				release_if_stale(gpu.name)
			if claim(gpu.name, self.name):
				taken.append(gpu.name)
				continue
			for name in taken:
				release(name, self.name)
			frappe.throw(
				f"GPU {gpu.gpu_index} on {self.inference_server} was taken by "
				f"{frappe.db.get_value('GPU', gpu.name, 'held_by')} first.",
				GPUUnavailable,
			)

	def release_gpus(self):
		"""Give this replica's cards back. Stopping releases on purpose: a stopped container holds
		no VRAM. Start re-takes them, and fails loudly if it cannot."""
		for name in frappe.get_all("GPU", filters={"held_by": self.name}, pluck="name"):
			release(name, self.name)

	@property
	def gpu_records(self):
		"""The `GPU` rows this replica pins, in the order it named them. A direct read of the
		links: the child row IS the reference."""
		names = [row.gpu for row in (self.gpus or []) if row.gpu]
		if not names:
			return []
		cards = {
			card.name: card
			for card in frappe.get_all(
				"GPU", filters={"name": ("in", names)},
				fields=["name", "gpu_index", "device_id", "held_by"],
			)
		}
		return [cards[name] for name in names if name in cards]

	def _assign_engine_port(self):
		"""Lowest free port from ENGINE_PORT_BASE up, scoped to THIS Inference Server — a box is
		multi-tenant, one port per deployment. Assigned once, then stable; teardown clears it so a
		later redeploy reallocates.

		Two replicas placed on one box at once would read the same siblings and pick the same
		port, so the box's row is locked first: the rival waits until this insert commits. The
		sibling read is a locking read as well — a plain SELECT answers from the transaction's
		snapshot, taken before the winner committed, and would still miss its port."""
		if self.engine_port or not self.inference_server:
			return
		frappe.db.get_value("Inference Server", self.inference_server, "name", for_update=True)
		# db.get_values, not get_all: only the former can take FOR UPDATE.
		used = {
			p
			for p in frappe.db.get_values(
				"Model Replica",
				{
					"inference_server": self.inference_server,
					"name": ["!=", self.name],
					"status": ["not in", _PORT_FREE_STATUSES],
				},
				"engine_port",
				for_update=True,
				pluck=True,
			)
			if p
		}
		port = ENGINE_PORT_BASE
		while port in used:
			port += 1
		self.engine_port = port

	@property
	def derived_engine_url(self):
		"""Where the gateway forwards: <box front>/e/<slug>, the front being a standalone box's fleet
		name or its public IP. nginx fronts every engine, so a box exposes one TLS port however many
		models it serves and engine_port stops being public.

		The one owner of the formula. deploy_model and reconfigure_deployment persist it only
		after a successful play, so a migrating deployment's URL never moves ahead of its
		route."""
		return f"{self.server.front_url}/e/{_instance_slug(self.name)}"

	def _derive_engine_url(self):
		"""Runs AFTER _assign_engine_port and before the mandatory check."""
		if not self.inference_server:
			return  # no box yet → let the reqd check flag engine_url
		self.engine_url = self.derived_engine_url

	def on_update(self):
		# A model is "published" only while it has a live deployment.
		if self.has_value_changed("status"):
			from grove.grove.doctype.model.model import sync_published

			sync_published(self.model)

	def after_insert(self):
		"""Claim the cards as soon as the row exists, while still Draft. Not in `validate`: a claim
		needs a name to hold the card with, and validate runs before there is one.

		This is the moment that closes the race — before it, two placements reading the same free
		list could both insert."""
		self.claim_gpus()

	def on_trash(self):
		# Exclude self — the row is still in the DB during on_trash.
		from grove.grove.doctype.model.model import sync_published

		self.release_gpus()
		sync_published(self.model, exclude=self.name)

	@frappe.whitelist()
	def setup(self):
		"""Button: (re)serve this model via the inference_server vllm role, then wire the gateway
		route."""
		frappe.enqueue(
			"grove.grove.doctype.model_replica.model_replica.deploy_model",
			queue="long",
			timeout=3600,
			model_replica=self.name,
		)
		frappe.msgprint(f"Deploying {self.model} on {self.inference_server} — watch its Ansible Plays.", alert=True)

	@frappe.whitelist()
	def apply_engine_config(self):
		"""Button: re-render the engine config and restart to apply edited per-box tuning. Fast
		path — config + restart tasks and nothing else. Replaces the container, so it drops
		in-flight requests."""
		frappe.enqueue(
			"grove.grove.doctype.model_replica.model_replica.reconfigure_deployment",
			queue="long",
			timeout=1200,
			model_replica=self.name,
		)
		frappe.msgprint(
			f"Re-rendering engine config on {self.inference_server} — watch its Ansible Plays.",
			alert=True,
		)

	@frappe.whitelist()
	def stop(self):
		"""Button: stop the container, leaving its run script, key and port on the box so Start
		brings the same engine back. Docker's restart policy is unless-stopped, so the stop holds
		across a reboot. → Inactive."""
		self.set_container_running(False)

	@frappe.whitelist()
	def start(self):
		"""Button: start the container Stop left on the box. → Active.

		Cards are re-taken first: Inactive released them, so a sibling may hold them now. Nothing
		about `docker start` would notice — two engines on one card split its VRAM and both OOM
		later — so the claim is taken here, where the refusal is readable."""
		self.claim_gpus()
		self.set_container_running(True)

	def set_container_running(self, running):
		"""Queue the container-state play. Played, not run inline: a lifecycle action that leaves
		no Ansible Play is one nobody can read back. Enqueued because `docker stop` waits out the
		engine's SIGTERM grace."""
		self.server  # resolved here so a missing box fails on the button, not in a worker
		frappe.enqueue(
			"grove.grove.doctype.model_replica.model_replica.set_container_state",
			queue="long",
			timeout=600,
			model_replica=self.name,
			running=running,
		)
		frappe.msgprint(
			f"{'Starting' if running else 'Stopping'} this instance on "
			f"{self.inference_server} — watch its Ansible Plays.",
			alert=True,
		)

	@frappe.whitelist()
	def teardown(self):
		"""Stop + Remove THIS deployment's container, run script and key file. Shared
		weights and the pulled image are left for other instances. → Terminated on success."""
		frappe.enqueue(
			"grove.grove.doctype.model_replica.model_replica.teardown_deployment",
			queue="long",
			timeout=600,
			model_replica=self.name,
		)
		frappe.msgprint(
			f"Tearing down this instance on {self.inference_server} — watch its Ansible Plays.",
			alert=True,
		)

	@property
	def container_name(self):
		return f"vllm-{_instance_slug(self.name)}"

	@property
	def server(self):
		if not self.inference_server:
			frappe.throw(f"Model Replica {self.name} has no Inference Server.")
		return frappe.get_doc("Inference Server", self.inference_server)

	@frappe.whitelist()
	def get_engine_logs(self, lines: int = 200):
		return self.server.run_command(
			["docker", "logs", "--tail", str(_log_lines(lines)), self.container_name]
		)

	@frappe.whitelist()
	def stream_engine_logs(self):
		"""`docker logs --follow` over SSH, relayed to this form for as long as it keeps
		pinging keep_streaming. Deduplicated per deployment, so a page reload does not double the
		stream."""
		from grove import log_relay

		self.server  # resolved here so a missing box fails on the button, not in a worker
		log_relay.keep_alive(self.doctype, self.name)
		frappe.enqueue(
			"grove.grove.doctype.model_replica.model_replica.stream_engine_logs",
			queue="long", timeout=1800, job_id=f"md-logs-{self.name}", deduplicate=True,
			model_replica=self.name,
		)

	@frappe.whitelist()
	def stop_engine_logs(self):
		from grove import log_relay

		log_relay.end(self.doctype, self.name)


def stream_engine_logs(model_replica):
	"""Job: follow the deployment's container log over SSH and relay it to its form."""
	from grove import log_relay

	md = frappe.get_doc("Model Replica", model_replica)
	command = ["docker", "logs", "--follow", "--tail", str(_log_lines(md.log_lines)), md.container_name]
	log_relay.relay(md.server.stream_command(command), md.doctype, md.name)


def parse_kv_cache_memory(log):
	"""The figure vLLM says reproduces this boot's allocation, smallest across ranks — vLLM sizes
	the cache off the tightest worker. The first of the two it prints: the second takes every
	free byte. 0 when no worker printed one (the hint was passed, or an older image)."""
	return min((int(figure) for figure in KV_CACHE_MEMORY_LINE.findall(log or "")), default=0)


def _log_lines(lines):
	"""Backfill size, clamped — `docker logs --tail` will happily read a whole disk."""
	return max(1, min(int(lines or 200), 5000))


def _instance_slug(md_name):
	"""Per-deployment slug, safe as a container name → vllm-<slug>, and the key file beside it."""
	return re.sub(r"[^a-z0-9._-]", "-", md_name.lower())


def _engine_env(md, engine, hf_token, streaming_env=None):
	"""The engine's own vars first, the operator's rows on top — the Pod path's precedence.

	No paths are handed over: on a box the run-script template writes the cache dirs and the role
	resolves VLLM_API_KEY. Key order is the env file's line order; see Engine.env."""
	env = engine.env(hf_token=hf_token, streaming_env=streaming_env)
	env.update({row.key: row.value or "" for row in md.deployment.engine_env_rows(md)})
	return env


def _vllm_extravars(md, m, inf, key):
	"""The vLLM Ansible extra-vars: Model (m) ⊕ deployment (md) ⊕ box (inf) ⊕ the internal key.
	Shared by deploy_model and reconfigure_deployment so the two paths cannot drift."""
	serve = md.engine

	# `GPU-<uuid>` for a whole card, `MIG-<uuid>` for a slice, or the bare index on a cloud box
	# seeded before any driver existed. A MIG slice HAS no index, so one here would address
	# nothing. Handed to `docker run --gpus`, not to CUDA_VISIBLE_DEVICES.
	devices = [card.device_id for card in md.gpu_records]

	hf_token = frappe.conf.get("hf_token", "")
	vllm_home = inf.data_path
	settings = frappe.get_single("Grove Settings")

	extravars = {
		"vllm_model": serve.repo,
		"vllm_served_name": md.model,
		"vllm_serve_args": serve.args,
		# Blank = no gate: a custom image that names none finishes the play once it starts.
		"vllm_health_path": md.deployment.health_path or serve.health_path,
		# One real request after the health gate, from the same source the args came from.
		"vllm_warmup_request": serve.warmup_request,
		# One container + port + key file per deployment: the box is multi-tenant.
		"vllm_instance": _instance_slug(md.name),
		"vllm_port": serve.port,
		"vllm_api_key": key,
		"vllm_cuda_visible_devices": ",".join(devices),
		"vllm_env": _engine_env(md, serve, hf_token, settings.weights_s3_engine_environment),
		"vllm_hf_token": hf_token,
		# Weights/caches on the mounted data volume, or the instance-store NVMe if opted in.
		"vllm_home": vllm_home,
		"vllm_hf_home": inf.hf_home,
		"vllm_cache_dir": f"{vllm_home}/cache",
		# The role derives the repo from vllm_model, so leaving this on for an image that brings
		# its own model would run `hf download` with no repo.
		"vllm_predownload_model": bool(serve.repo) and not serve.is_streaming,
		# The free-space check, before either download starts. 0 reads as "cannot check".
		"vllm_weights_gb": serve.weights_gb,
		# Compile-cache pre-warm; blank bucket turns the hooks off. The key's other axes (image
		# digest, GPU) are computed on the box.
		"vllm_cache_bucket": (settings.weights_bucket or "") if serve.repo else "",
		"vllm_cache_sync_env": settings.weights_s3_engine_environment,
		"vllm_tensor_parallel_size": serve.tensor_parallel_size,
		"vllm_model_slug": (m.hf_repo or md.model).split(":")[0].replace("/", "--"),
		# serve.yml runs grove_https and engine_proxy ahead of the vllm role, so it writes the
		# box's htpasswd too. Unused by reconfigure.yml, which runs neither role.
		**settings.scrape_auth_variables,
		# serve.yml re-renders nginx: without these a standalone box falls back to box.crt.
		**inf.tls_variables,
	}
	image = frappe.get_cached_doc("Engine Image", md.deployment.engine_image)
	extravars["vllm_image"] = image.full_image
	extravars["vllm_image_gb"] = image.size_gb or 0
	# The proxy authenticates nothing itself, and an image that serves itself enforces no key of
	# ours — so the proxy has to check the bearer for that kind. This is what tells it to.
	extravars["vllm_engine_kind"] = image.engine_kind
	if credentials := image.registry_credentials:
		extravars["vllm_registry_host"] = image.registry_host
		extravars["vllm_registry_username"], extravars["vllm_registry_token"] = credentials
	return extravars


@failure.reports_failure(mark_broken=True, doctype="Model Replica")
def deploy_model(model_replica):
	"""Serve a Model Replica via the inference_server vllm role. Every arg is passed as Ansible
	extra-vars — the doctypes are the source of truth, not a hand-written group_vars. On success
	→ Active + push the routing table."""
	md = frappe.get_doc("Model Replica", model_replica)
	m = frappe.get_doc("Model", md.model)
	inf = md.server
	if not inf.is_provisioned:
		frappe.throw(
			f"Inference Server {inf.name} is not provisioned — run its Setup "
			"(host bootstrap) before deploying a model onto it."
		)
	# No claim check here: the cards were taken at insert and held for every claiming status.

	# Generated once and served with, so the gateway's stored copy always matches.
	key = md.get_password("internal_api_key", raise_exception=False)
	# Teardown frees the port with db.set_value, which skips validate, so a redeploy arrives
	# holding 0. Saving is what reallocates it and re-derives engine_url — without this the
	# container publishes port 0 while the doc advertises the old one.
	needs_save = not md.engine_port
	if not key:
		key = secrets.token_hex(24)
		md.internal_api_key = key
		needs_save = True
	if needs_save:
		md.save(ignore_permissions=True)
		frappe.db.commit()

	extravars = _vllm_extravars(md, m, inf, key)

	frappe.db.set_value("Model Replica", md.name, "status", "Provisioning")
	frappe.db.commit()

	play_name, rc = inf.run_playbook(
		"serve.yml",
		extravars=extravars,
		reference_doctype="Model Replica",
		reference_docname=md.name,
	)

	state = _post_play_state(md, rc)
	frappe.db.set_value("Model Replica", md.name, state)
	# db.set_value skips the controller on_update, so the published flag and the GPU claims are
	# settled explicitly. Status is carried over rather than reloaded: it was just written from
	# this same dict.
	from grove.grove.doctype.model.model import sync_published

	md.status = state["status"]
	md.sync_gpu_claims()
	sync_published(md.model)
	frappe.db.commit()
	return play_name, rc


@failure.reports_failure(mark_broken=True, doctype="Model Replica")
def reconfigure_deployment(model_replica):
	"""Re-render the engine config and restart it for an already-served deployment.

	reconfigure.yml is the vllm role's config tasks and nothing else — no disk check, image pull,
	predownload or proxy/TLS roles — so it assumes a deploy already saw to those. That is what
	makes it runnable while a deploy sits on the health gate: a flag typo is fixed by
	re-rendering and restarting, not by waiting the gate out.

	Replaces the container when the rendered config moved, so it drops in-flight requests and is
	button-triggered. Stays Active for the run; see the note below the extra-vars."""
	md = frappe.get_doc("Model Replica", model_replica)
	m = frappe.get_doc("Model", md.model)
	inf = md.server
	if not inf.is_provisioned:
		frappe.throw(f"Inference Server {inf.name} is not provisioned — deploy the model first.")

	key = md.get_password("internal_api_key", raise_exception=False)
	if not key:
		frappe.throw(
			f"Model Replica {md.name} has no internal key — run a full Deploy "
			"before reconfiguring."
		)

	extravars = _vllm_extravars(md, m, inf, key)

	# Status is deliberately NOT moved to Provisioning here, unlike deploy_model. _gateway_routes
	# only routes Active, so flipping it for the play would dark the model for MINUTES — to cover
	# a restart that is seconds long and often does not happen at all, since the container is
	# replaced only when the rendered config moved.
	play_name, rc = inf.run_playbook(
		"reconfigure.yml",
		extravars=extravars,
		reference_doctype="Model Replica",
		reference_docname=md.name,
	)

	state = _post_play_state(md, rc)
	frappe.db.set_value("Model Replica", md.name, state)
	# Both arrive here as a status the controller never saw, so claims are settled explicitly.
	md.status = state["status"]
	md.sync_gpu_claims()
	frappe.db.commit()
	return play_name, rc


def _post_play_state(md, rc):
	"""What a finished serve play leaves on the doc. engine_url moves only on success: a failed
	play may not have written the box's nginx location, so the URL never moves ahead of the route
	it names. Not md.save(): a full validate can throw on drift unrelated to this deploy, after
	the play already succeeded."""
	if rc != 0:
		state = {"status": "Broken"}
		if md.engine.kv_cache_memory:
			# vLLM's remedy for a boot that OOMs under the hint: drop it and profile again.
			state.update(kv_cache_memory=0, kv_cache_memory_for="")
		return state
	return {"status": "Active", "engine_url": md.derived_engine_url, **md.learned_kv_cache_memory()}


@failure.reports_failure(mark_broken=False, doctype="Model Replica")
def set_container_state(model_replica, running):
	"""Job: start or stop ONE deployment's container. Not mark_broken: a stop that failed is an
	engine still serving, which is what the doc already claims.

	Only rc 0 moves the status, because `status` is what _gateway_routes routes on — writing it
	ahead of the play would publish a stopped engine, or dark a running one."""
	md = frappe.get_doc("Model Replica", model_replica)
	inf = md.server

	play_name, rc = inf.run_playbook(
		"container_state.yml",
		extravars={
			"vllm_instance": _instance_slug(md.name),
			"vllm_container_running": bool(running),
		},
		reference_doctype="Model Replica",
		reference_docname=md.name,
	)

	if rc == 0:
		frappe.db.set_value(
			"Model Replica", md.name, "status", "Active" if running else "Inactive"
		)
		from grove.grove.doctype.model.model import sync_published

		sync_published(md.model)
		frappe.db.commit()
	return play_name, rc


@failure.reports_failure(mark_broken=False, doctype="Model Replica")
def teardown_deployment(model_replica):
	"""Stop + remove ONE deployment's container, the run script and env file that would restart
	it, and its key file. Leaves the box-shared caches and the pulled image for other instances.
	On success → Terminated."""
	md = frappe.get_doc("Model Replica", model_replica)
	inf = md.server

	play_name, rc = inf.run_playbook(
		"teardown.yml",
		extravars={
			"vllm_instance": _instance_slug(md.name),
			"vllm_home": inf.data_path,
		},
		reference_doctype="Model Replica",
		reference_docname=md.name,
	)

	if rc == 0:
		# 0 = free, reallocated on a later redeploy. The Int column is NOT NULL, so 0 not None.
		frappe.db.set_value(
			"Model Replica", md.name, {"status": "Terminated", "engine_port": 0}
		)
		from grove.grove.doctype.model.model import sync_published

		sync_published(md.model)
		frappe.db.commit()
	return play_name, rc
