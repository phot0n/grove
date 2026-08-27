# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re
import secrets

import frappe
from frappe.model.document import Document

from grove import failure
from grove.grove.doctype.gpu_claim.gpu_claim import claim_name, release_if_stale
from grove.naming import next_replica_name
from grove.utils import is_env_key, is_env_value


ENGINE_PORT_BASE = 8080
# Only teardown takes the container off the box, so only Terminated releases the port. Inactive
# keeps its: Start expects the same one back, and an unused port costs the box nothing.
_PORT_FREE_STATUSES = ("Terminated",)
# Statuses whose deployment still owns its GPUs. Provisioning is one of them: deploy_model
# checks the claim BEFORE it flips the status, so a second deploy started during the first
# one's play would find the cards free and land on them too. Broken is another —
# --restart unless-stopped means a crash-looping engine keeps coming back onto its cards.
# Inactive is not: VRAM is not a reservation and a stopped container holds none of it, so its
# cards are offered to siblings and Start re-checks before it puts an engine back on them.
GPU_CLAIMING_STATUSES = ("Provisioning", "Active", "Broken")
# What HOLDS a GPU Claim, which is the serving set plus Draft. A replica takes its cards the
# moment its row exists, before anything is deployed — that reservation is what makes two
# concurrent placements impossible rather than merely unlikely, since the second one cannot
# insert the claim. The cost is that an abandoned Draft strands its cards until it is deleted,
# which the allocation panel shows by name.
CLAIM_HOLDING_STATUSES = ("Draft", *GPU_CLAIMING_STATUSES)


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
		gpu_memory_utilization: DF.Float
		gpus: DF.Table[ModelReplicaGPU]
		inference_server: DF.Link
		internal_api_key: DF.Password | None
		kv_cache_dtype: DF.Literal["", "auto", "fp8"]
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
		"""The Model Deployment this replica was placed from — what it serves, on what shape,
		with which tuning. This doc owns only WHERE: the box, the cards, the port."""
		if not self.model_deployment:
			frappe.throw(f"Model Replica {self.name} has no Model Deployment.")
		return frappe.get_cached_doc("Model Deployment", self.model_deployment)

	@property
	def engine(self):
		"""The Engine this replica runs: its deployment's shape and tuning with this replica's own
		overrides applied, its GPU rows for the count and its port. Built BY the deployment, so the
		preview shown there and the command that runs here cannot be computed two ways."""
		return self.deployment.engine_for(self)

	def autoname(self):
		"""`<model id>-<region>-<server>-<n>`, e.g. `qwen3-8b-ap-south-1-inf3-00007` (`grove/naming.py`) — what it serves, where, and off
		which box, readable from a list row.

		Model is read off the Model Deployment and region off the Inference Server, because
		NEITHER field's `fetch_from` has run this early — a replica created from a deployment (which
		sends only the deployment, the box and the cards) would otherwise name itself with a blank
		model and fail its own mandatory check. Assigning `model` here is what makes it set by the
		time anything else on this doc reads it."""
		self.model = frappe.db.get_value("Model Deployment", self.model_deployment, "model")
		region = frappe.db.get_value("Inference Server", self.inference_server, "region")
		self.name = next_replica_name(self.model, self.inference_server, region)

	def validate(self):
		self._assign_engine_port()
		self._derive_engine_url()
		self._validate_gpus()
		self._validate_engine()

	def _validate_engine(self):
		"""The env rows that go into this replica's container. They are rendered into a docker
		--env-file, which is a trust boundary — a newline in a value would otherwise append a
		variable of the operator's choosing."""
		self._validate_engine_architecture()
		for row in self.env or []:
			if not is_env_key(row.key):
				frappe.throw(f"'{row.key}' is not a valid environment variable name.")
			if not is_env_value(row.value):
				frappe.throw(f"Value for '{row.key}' cannot contain a newline or a double quote.")

	def _validate_engine_architecture(self):
		"""The image and the box it runs on have to be the same architecture. Docker pulls the
		wrong one happily and fails at exec, deep inside a play, with nothing that names the
		cause. A box with no architecture recorded is on-prem — nothing to check it against."""
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

	# ── GPU pinning ───────────────────────────────────────────────────────────
	# The box's cards come from its Inference Server. A deployment names the CUDA indices it
	# wants (self.gpus); validate() checks they exist and fills the display columns. No GPU
	# rows → single-GPU, unpinned (back-compat). No cross-deployment allocation tracking —
	# the operator assigns indices deliberately.

	@property
	def gpu_vram_gb(self):
		"""VRAM per pinned GPU, taken as the smallest of them — a mixed box is capped by its
		smallest card. None until the box's GPU inventory carries a VRAM figure."""
		sizes = [row.vram_gb for row in self.gpus or [] if row.vram_gb]
		return min(sizes) if sizes else None

	def _validate_gpus(self):
		"""Reject duplicate, unknown or off-shape GPUs, fill each row's display columns from the
		box's GPU inventory, and rebuild the serve command preview."""
		seen = set()
		for r in self.gpus or []:
			if r.gpu_index in seen:
				frappe.throw(f"GPU index {r.gpu_index} is listed twice.")
			seen.add(r.gpu_index)

		# Fill the display columns first — the placement check reads vram_gb off these rows.
		# No box yet → the reqd check on inference_server flags it; the split is still checked.
		if self.gpus and self.inference_server:
			on_box = {int(gpu.gpu_index): gpu for gpu in self.server.gpus}
			for r in self.gpus:
				gpu = on_box.get(int(r.gpu_index))
				if not gpu:
					frappe.throw(
						f"Inference Server {self.inference_server} has no GPU with CUDA "
						f"index {r.gpu_index}."
					)
				r.gpu_model = gpu.gpu_model
				r.vram_gb = gpu.vram_gb

		self._validate_shape()

		engine = self.engine
		if errors := engine.placement_errors:
			frappe.throw("<br>".join(errors))
		# Store what actually reaches --max-model-len. The suffix is input sugar, so a doc that
		# kept '128k' would leave the real number derivable only by re-parsing it. Blank stays
		# blank — that is how a placement asks for the engine default.
		if self.max_model_len:
			self.max_model_len = str(engine.max_model_len)
		# The preview, from the same builder the deploy uses — so what is shown is what runs. The
		# only rendered field left here: it carries this replica's port and its overrides, so it
		# genuinely differs from the deployment's. Tensor parallel size does not, which is why that
		# one is read off the deployment rather than stored again.
		self.serve_command = engine.command

	def _validate_shape(self):
		"""A replica has to take as many cards as its deployment declares. That is what makes
		replicas of one deployment interchangeable — the aggregate a scheduler or an autoscaler
		divides by is `replicas x capacity`, which is not arithmetic if they are different sizes.
		It is also what lets the deployment's tensor parallel size be the only one there is.

		A replica naming no GPUs at all is the unpinned single-GPU case and is left alone."""
		declared = self.deployment.gpus_per_replica
		if self.gpus and declared and len(self.gpus) != declared:
			frappe.throw(
				f"{self.model_deployment} places replicas on {declared} GPU(s), but this one "
				f"names {len(self.gpus)}. Pin {declared} of them, or use a deployment with a "
				"shape that matches this box."
			)

	def sync_gpu_claims(self):
		"""Make the GPU Claim rows match what this replica's status says it should hold.

		The one place the claim rule lives. A claiming status takes a `GPU Claim` per pinned card,
		whose NAME is `<box>:<index>` — so the database, not a check, is what stops two replicas
		holding one card: the second insert cannot happen. Anything else releases.

		Called after every status transition rather than from `validate`, because status moves by
		`db.set_value`, which never runs validate."""
		if self.status in CLAIM_HOLDING_STATUSES:
			self.claim_gpus()
		else:
			self.release_gpus()

	def claim_gpus(self):
		"""Take a GPU Claim for each pinned card. Raises `frappe.DuplicateEntryError` if a sibling
		genuinely holds one — that is the race being lost, and the caller decides what to do about
		it (placement moves to the next box; Start refuses on the button).

		A claim left behind by a replica that is no longer entitled to it is cleared first. Stored
		ownership can drift where the derived kind could not — a worker dying between the status
		flip and the release strands the card forever — so the moment someone else wants it is
		where that gets repaired."""
		machine = self.server.machine
		held = set(frappe.get_all("GPU Claim", filters={"model_replica": self.name}, pluck="gpu_index"))
		for row in self.gpus or []:
			index = int(row.gpu_index)
			if index in {int(i) for i in held}:
				continue
			name = claim_name(machine, index)
			if frappe.db.exists("GPU Claim", name):
				release_if_stale(name)
			frappe.get_doc(
				{
					"doctype": "GPU Claim",
					"machine": machine,
					"inference_server": self.inference_server,
					"gpu_index": index,
					"model_replica": self.name,
				}
			).insert(ignore_permissions=True)

	def release_gpus(self):
		"""Give this replica's cards back. Stopping releases on purpose — a stopped container
		holds no VRAM, so the cards are genuinely free and a sibling may take them. Start is what
		re-takes them, and fails loudly if it cannot."""
		for name in frappe.get_all("GPU Claim", filters={"model_replica": self.name}, pluck="name"):
			frappe.delete_doc("GPU Claim", name, ignore_permissions=True, force=True)

	def _assign_engine_port(self):
		"""Allocate a box-local vLLM port once (multi-tenant box: one port per
		deployment). Lowest free port from ENGINE_PORT_BASE up, scoped to THIS
		Inference Server, skipping ports held by non-freed deployments. Ports released
		by teardown (status Terminated + port cleared) are reused. Assigned
		once, then stable — teardown clears it so a later redeploy reallocates."""
		if self.engine_port or not self.inference_server:
			return
		used = {
			p
			for p in frappe.get_all(
				"Model Replica",
				filters={
					"inference_server": self.inference_server,
					"name": ["!=", self.name or ""],
					"status": ["not in", _PORT_FREE_STATUSES],
				},
				pluck="engine_port",
			)
			if p
		}
		port = ENGINE_PORT_BASE
		while port in used:
			port += 1
		self.engine_port = port

	@property
	def derived_engine_url(self):
		"""Where the gateway forwards: this deployment's location on its box's engine proxy,
		https://<machine public ip>/e/<slug>. No LiteLLM front — vLLM 0.24+ serves /v1/messages
		natively and reports the prefix cache — but nginx does front it, so a box exposes one
		TLS port however many models it serves, and engine_port stops being public.

		Derived, never hand-typed, and the one owner of the formula: deploy_model and
		reconfigure_deployment persist it after a successful play, which is how an existing
		deployment migrates without its URL moving before its box has the route."""
		machine_ip = frappe.db.get_value("Inference Server", self.inference_server, "machine_ip")
		if not machine_ip:
			frappe.throw(
				f"Inference Server {self.inference_server} has no machine IP "
				"(set its Machine's public IP) — cannot derive Engine URL."
			)
		return f"https://{machine_ip}/e/{_instance_slug(self.name)}"

	def _derive_engine_url(self):
		"""Runs AFTER _assign_engine_port and before the mandatory check, so both the port and
		this reqd field are satisfied here."""
		if not self.inference_server:
			return  # no box yet → let the reqd check flag engine_url
		self.engine_url = self.derived_engine_url

	def on_update(self):
		# A model is "published" only while it has a live deployment — keep the
		# parent Model in sync whenever this deployment's status changes.
		if self.has_value_changed("status"):
			from grove.grove.doctype.model.model import sync_published

			sync_published(self.model)

	def after_insert(self):
		"""Claim the cards as soon as the row exists, while still Draft.

		Not in `validate`: a claim needs a name to hold the card with, and validate runs before
		there is one. This is the moment that closes the race — before it, two placements reading
		the same free list could both insert; after it, the second insert cannot happen."""
		self.claim_gpus()

	def on_trash(self):
		# Deleting this deployment may drop the model's last Active placement.
		# Exclude self — the row is still in the DB during on_trash.
		from grove.grove.doctype.model.model import sync_published

		self.release_gpus()
		sync_published(self.model, exclude=self.name)

	@frappe.whitelist()
	def setup(self):
		"""Button: (re)serve this deployment's model on its Inference Server via
		the playbooks/inference_server vllm role (args from the Model launch profile ⊕ this doc), then
		wire the gateway route."""
		frappe.enqueue(
			"grove.grove.doctype.model_replica.model_replica.deploy_model",
			queue="long",
			timeout=3600,
			model_replica=self.name,
		)
		frappe.msgprint(f"Deploying {self.model} on {self.inference_server} — watch its Ansible Plays.", alert=True)

	@frappe.whitelist()
	def apply_engine_config(self):
		"""Button: re-render this deployment's engine config and restart it to apply edited
		per-box tuning (kv cache dtype / gpu_memory_utilization / batch caps / attention backend
		/ env rows). Fast path — its own playbook, which is the config + restart tasks and
		nothing else. Replaces the container, so it drops in-flight requests."""
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
		"""Button: stop this engine's container, leaving it — with its run script, key and
		port — on the box, so Start brings the same engine back without a redeploy. Docker's
		restart policy is unless-stopped, so the stop holds across a reboot. → Inactive."""
		self.set_container_running(False)

	@frappe.whitelist()
	def start(self):
		"""Button: start the container Stop left on the box. → Active.

		The cards are re-taken first: Inactive released them, so a sibling may have been placed on
		them while this was stopped. Nothing about `docker start` would notice — two engines on one
		card split its VRAM and both OOM later, at a size each thought it had — so the claim is
		taken on the button, where a DuplicateEntryError is something an operator can read."""
		try:
			self.claim_gpus()
		except frappe.DuplicateEntryError:
			frappe.throw(
				f"A GPU this replica needs on {self.inference_server} was taken while it was "
				"stopped. Free it, or place this replica somewhere else."
			)
		self.set_container_running(True)

	def set_container_running(self, running):
		"""Queue the container-state play. Played rather than run inline: this changes the box,
		which is a role's job and not run_command's, and a lifecycle action that leaves no
		Ansible Play is one nobody can read back afterwards. Enqueued for the same reason
		Teardown is — `docker stop` waits out the engine's SIGTERM grace."""
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
		"""Button: stop + remove THIS deployment's container, its run script and its key file
		from the box (multi-tenant teardown). Shared weights and the pulled image are left for
		other instances. → Terminated on success."""
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
		"""This deployment's container on its box. One instance per Model Replica, so a box
		can run many at once."""
		return f"vllm-{_instance_slug(self.name)}"

	@property
	def server(self):
		"""The Inference Server this deployment runs on — its only route to the box."""
		if not self.inference_server:
			frappe.throw(f"Model Replica {self.name} has no Inference Server.")
		return frappe.get_doc("Inference Server", self.inference_server)

	@frappe.whitelist()
	def get_engine_logs(self, lines: int = 200):
		"""Button: this engine container's own log, read off its box. Where a failed deploy
		explains itself: the Ansible Play only says the health gate timed out, the engine says
		why it never came up. Read-only and not stored on the doc; a crash-looping container's
		log is the whole point and it changes every few seconds."""
		return self.server.run_command(
			["docker", "logs", "--tail", str(_log_lines(lines)), self.container_name]
		)

	@frappe.whitelist()
	def stream_engine_logs(self):
		"""Button: follow this engine container's log off its box (`docker logs --follow` over
		SSH), relayed to this form for as long as it keeps pinging keep_streaming. Deduplicated
		per deployment, so a second Start — after a page reload, say — does not double up the
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
		"""Tell the streaming job to finish — it checks between publishes."""
		from grove import log_relay

		log_relay.end(self.doctype, self.name)


def stream_engine_logs(model_replica):
	"""Job: follow the deployment's container log over SSH and relay it to its form."""
	from grove import log_relay

	md = frappe.get_doc("Model Replica", model_replica)
	command = ["docker", "logs", "--follow", "--tail", str(_log_lines(md.log_lines)), md.container_name]
	log_relay.relay(md.server.stream_command(command), md.doctype, md.name)


def _log_lines(lines):
	"""Backfill size, clamped — `docker logs --tail` will happily read a whole disk."""
	return max(1, min(int(lines or 200), 5000))


def _instance_slug(md_name):
	"""Per-deployment slug, safe as a container name → vllm-<slug>, and the key file beside
	it. One instance per Model Replica so a box can run many at once."""
	return re.sub(r"[^a-z0-9._-]", "-", md_name.lower())


def _engine_env(md, engine, hf_token, streaming_env=None):
	"""Env vars for the engine container: what the engine needs first, the operator's own rows
	layered on top (same precedence the Pod path uses).

	No paths are handed over — on a box the run-script template writes the cache dirs and the role
	resolves VLLM_API_KEY on the box, so the engine names neither. Key order is the env file's line
	order; see Engine.env."""
	env = engine.env(hf_token=hf_token, streaming_env=streaming_env)
	env.update({row.key: row.value or "" for row in md.deployment.engine_env_rows(md)})
	return env


def _vllm_extravars(md, m, inf, key):
	"""Assemble the vLLM Ansible extra-vars from the Model launch profile (m) ⊕ this
	deployment (md) ⊕ its box (inf) ⊕ the internal key. Shared by deploy_model (full serve)
	and reconfigure_deployment (config-only) so the two paths can never drift. The arguments come
	from the deployment's Engine — the same builder the Pod path uses; the run script on the box
	only renders them, positional unquoted and every flag quoted."""
	serve = md.engine

	# GPU pinning: the deployment names CUDA indices on its box (md.gpus). N GPUs →
	# tensor-parallel across exactly those, with CUDA_VISIBLE_DEVICES so vLLM only
	# sees them. No rows → single-GPU, unpinned (whatever the box exposes).
	gpu_indexes = sorted(int(r.gpu_index) for r in (md.gpus or []))

	hf_token = frappe.conf.get("hf_token", "")
	vllm_home = inf.data_path
	settings = frappe.get_single("Grove Settings")

	extravars = {
		"vllm_model": serve.repo,
		"vllm_served_name": md.model,
		"vllm_serve_args": serve.args,
		# Blank = no gate. vLLM's own /health unless this deployment names a path; a custom image
		# that names none finishes the play as soon as its container starts.
		"vllm_health_path": md.deployment.health_path or serve.health_path,
		# One real request after the health gate, from the same source the args came from.
		"vllm_warmup_request": serve.warmup_request,
		# One container + port + key file per deployment (multi-tenant box). Slug from the
		# MD name → container vllm-<instance> and its run script beside the key.
		"vllm_instance": _instance_slug(md.name),
		"vllm_port": serve.port,
		"vllm_api_key": key,
		"vllm_cuda_visible_devices": ",".join(str(i) for i in gpu_indexes),
		"vllm_env": _engine_env(md, serve, hf_token, settings.weights_s3_engine_environment),
		"vllm_hf_token": hf_token,
		# Weights/caches on the mounted data volume, or the instance-store NVMe if opted in.
		"vllm_home": vllm_home,
		"vllm_hf_home": inf.hf_home,
		"vllm_cache_dir": f"{vllm_home}/cache",
		# Nothing of ours to fetch for an image that brings its own model — and the role derives
		# the repo from vllm_model, so leaving this on would run `hf download` with no repo.
		"vllm_predownload_model": bool(serve.repo) and not serve.is_streaming,
		# What the role checks the box's free space against, before either download starts.
		# 0 means the figure was never fetched, which the role reads as "cannot check".
		"vllm_weights_gb": serve.weights_gb,
		# S3 compile-cache pre-warm: blank bucket turns the hooks off. The key's other axes
		# (image digest, GPU) are computed on the box — only TP and the model come from here.
		"vllm_cache_bucket": (settings.weights_bucket or "") if serve.repo else "",
		"vllm_cache_sync_env": settings.weights_s3_engine_environment,
		"vllm_tensor_parallel_size": serve.tensor_parallel_size,
		"vllm_model_slug": (m.hf_repo or md.model).split(":")[0].replace("/", "--"),
		# serve.yml runs grove_https and engine_proxy ahead of the vllm role, so that play
		# writes the box's htpasswd too — from the same source provision.yml reads. Unused by
		# reconfigure.yml, which runs neither role.
		**settings.scrape_auth_variables,
	}
	image = frappe.get_cached_doc("Engine Image", md.deployment.engine_image)
	extravars["vllm_image"] = image.full_image
	extravars["vllm_image_gb"] = image.size_gb or 0
	# The box's engine proxy is the only thing in front of the container, and it authenticates
	# nothing itself — vLLM enforces VLLM_API_KEY, an image that serves itself enforces nothing.
	# So the proxy has to check the bearer for that kind, and this is what tells it to.
	extravars["vllm_engine_kind"] = image.engine_kind
	if credentials := image.registry_credentials:
		extravars["vllm_registry_host"] = image.registry_host
		extravars["vllm_registry_username"], extravars["vllm_registry_token"] = credentials
	return extravars


@failure.reports_failure(mark_broken=True, doctype="Model Replica")
def deploy_model(model_replica):
	"""Serve a Model Replica on its Inference Server via the playbooks/inference_server vllm role.
	Every vLLM arg is assembled from the Model launch profile ⊕ this deployment
	(§8E) and passed as Ansible extra-vars — the DocTypes are the source of truth,
	not a hand-written group_vars. On success → Active + push the routing table."""
	md = frappe.get_doc("Model Replica", model_replica)
	m = frappe.get_doc("Model", md.model)
	inf = md.server
	if not inf.is_provisioned:
		frappe.throw(
			f"Inference Server {inf.name} is not provisioned — run its Setup "
			"(host bootstrap) before deploying a model onto it."
		)
	# No claim check here: the cards were taken when the row was inserted and are held for
	# every claiming status, so nothing can have moved onto them since.

	# Grove owns the internal key: generate once and serve with it, so the gateway
	# (which stores it as the deployment's internal_api_key) always matches.
	key = md.get_password("internal_api_key", raise_exception=False)
	# Teardown frees the port with db.set_value, which skips validate — so a redeploy
	# arrives here holding 0, and saving is what runs _assign_engine_port again and
	# re-derives engine_url from it. Without this the container publishes port 0 and the
	# doc still advertises the old one.
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
	# db.set_value skips the controller on_update — recompute the model's
	# published flag (Active deployment → publishable, Broken → maybe unpublish),
	# and settle the GPU claims against whatever status the play left behind. The status is
	# carried over rather than reloaded: it was just written from this same dict.
	from grove.grove.doctype.model.model import sync_published

	md.status = state["status"]
	md.sync_gpu_claims()
	sync_published(md.model)
	frappe.db.commit()
	return play_name, rc


@failure.reports_failure(mark_broken=True, doctype="Model Replica")
def reconfigure_deployment(model_replica):
	"""Re-render the engine config and restart it for an already-served deployment, applying
	edited per-box tuning (kv cache dtype / gpu_memory_utilization / batch caps /
	attention backend / env rows).

	Runs reconfigure.yml — the vllm role's config tasks and nothing else: no disk check, no
	image pull, no weights predownload, no proxy/TLS roles. Assumes the box is provisioned, the
	image is on it and the weights are in its shared cache, which a deploy already saw to. That
	is what makes this runnable while a deploy is still sitting on the health gate: a flag typo
	is fixed by re-rendering the run script and restarting, not by waiting the gate out.

	Replaces the engine container when the rendered config actually moved → drops in-flight
	requests, so it's button-triggered, not automatic. The deployment stays Active for the run;
	see the note below the extra-vars."""
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

	# Status is deliberately NOT moved to Provisioning here, unlike deploy_model.
	#
	# It is read as "is this engine serving?" — _gateway_routes only routes Active — and the
	# scheduler pushes the full route table every 2 minutes. Flipping it for the duration of the
	# play therefore takes the model out of the gateway for MINUTES, and this play usually does
	# not stop the engine at all: the container is replaced only when the run script or env file
	# renders differently, so a re-run that changes nothing leaves it serving throughout. Paying a
	# guaranteed multi-minute outage to cover a restart that is seconds long and often does not
	# happen is the wrong trade. A run that does replace the container has a gap no route table
	# could have hidden anyway.
	play_name, rc = inf.run_playbook(
		"reconfigure.yml",
		extravars=extravars,
		reference_doctype="Model Replica",
		reference_docname=md.name,
	)

	state = _post_play_state(md, rc)
	frappe.db.set_value("Model Replica", md.name, state)
	# Stopping releases the cards and Terminated gives them up for good; both arrive here as a
	# status the controller never saw, so the claims are settled explicitly.
	md.status = state["status"]
	md.sync_gpu_claims()
	frappe.db.commit()
	return play_name, rc


def _post_play_state(md, rc):
	"""What a finished serve play leaves on the doc. engine_url moves only on success: a failed
	play may not have written the box's nginx location, and the gateway would then forward to a
	route that is not there.

	This is also the migration. A deployment served before the engine proxy existed keeps its
	old http://<ip>:<port> until its next Deploy or Update Engine Config — the same run that puts
	the location on the box — so the URL never moves ahead of the route it names. Not md.save():
	a full validate can throw on drift unrelated to this deploy after the play already
	succeeded."""
	if rc != 0:
		return {"status": "Broken"}
	return {"status": "Active", "engine_url": md.derived_engine_url}


@failure.reports_failure(mark_broken=False, doctype="Model Replica")
def set_container_state(model_replica, running):
	"""Job: start or stop ONE deployment's container, leaving everything else it holds on the
	box. Not mark_broken: a stop that failed is an engine still serving, which is the state the
	doc already claims — Broken would take it out of the route table over a button that did
	nothing.

	The play proves the container reached the state, so only rc 0 moves the status. That
	ordering is the point: `status` is what _gateway_routes routes on, so writing it ahead of
	the play would publish a stopped engine, or dark a running one."""
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
	"""Stop + remove ONE deployment's container, the run script and env file that would
	restart it, and its key file (multi-tenant teardown). Leaves the box-shared
	weights/compile caches and the pulled image intact — other instances may use them.
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
		# Release the box-local port (0 = free → reallocated on a later redeploy;
		# the Int column is NOT NULL, so 0 not None).
		frappe.db.set_value(
			"Model Replica", md.name, {"status": "Terminated", "engine_port": 0}
		)
		from grove.grove.doctype.model.model import sync_published

		sync_published(md.model)
		frappe.db.commit()
	return play_name, rc
