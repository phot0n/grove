# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re
import secrets

import frappe
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.utils import ansible_project_dir, is_env_key, is_env_value
from grove.serve_command import ServeCommand


ENGINE_PORT_BASE = 8080
# Statuses whose deployment no longer runs an engine on the box → its port is
# free to reuse. Everything else (Draft/Provisioning/Active/Broken) reserves its port.
_PORT_FREE_STATUSES = ("Inactive", "Terminated")


class ModelDeployment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.model_deployment_gpu.model_deployment_gpu import ModelDeploymentGPU
		from grove.grove.doctype.pod_env.pod_env import PodEnv

		aliases: DF.SmallText | None
		allow_long_max_model_len: DF.Check
		attention_backend: DF.Literal["auto", "FLASH_ATTN", "XFORMERS", "FLASHINFER"]
		dtype: DF.Literal["auto", "float16", "bfloat16"]
		engine_image: DF.Link
		engine_port: DF.Int
		engine_url: DF.Data | None
		env: DF.Table[PodEnv]
		extra_serve_args: DF.SmallText | None
		gpu_memory_utilization: DF.Float
		gpus: DF.Table[ModelDeploymentGPU]
		inference_server: DF.Link
		internal_api_key: DF.Password | None
		log_lines: DF.Int
		max_model_len: DF.Int
		model: DF.Link
		pipeline_parallel_size: DF.Int
		region: DF.Link | None
		status: DF.Literal["Draft", "Provisioning", "Active", "Inactive", "Terminated", "Broken"]
		tensor_parallel_size: DF.Int
	# end: auto-generated types

	# Routes are not dirty-gated: grove.gateway_sync.sync_dirty pushes the full
	# route table for every deployment each run (idempotent), so no on_update
	# hook is needed for routing.

	def validate(self):
		self._assign_engine_port()
		self._derive_engine_url()
		self._validate_gpus()
		self._validate_engine()

	def _validate_engine(self):
		"""The image that serves this deployment and the env rows that go into it. Those rows
		are rendered into a docker --env-file, which is a trust boundary — a newline in a
		value would otherwise append a variable of the operator's choosing."""
		# The image is frozen once deployed: swapping it while the old container still holds
		# the port would have the new one fail to bind, and status never returns to Draft, so
		# this reads as set-only-once-after-deploy. A new deployment is the way to switch.
		if not self.is_new() and self.status != "Draft" and self.has_value_changed("engine_image"):
			frappe.throw(
				f"Engine Image cannot change after deployment (this one is {self.status}). "
				"Create a new Model Deployment to serve from a different image."
			)
		for row in self.env or []:
			if not is_env_key(row.key):
				frappe.throw(f"'{row.key}' is not a valid environment variable name.")
			if not is_env_value(row.value):
				frappe.throw(f"Value for '{row.key}' cannot contain a newline or a double quote.")

	# ── GPU pinning ───────────────────────────────────────────────────────────
	# GPUs live as child rows on the box's Machine (Machine GPU). A deployment names the
	# CUDA indices it wants (self.gpus); validate() checks they exist and fills the display
	# columns. No GPU rows → single-GPU, unpinned (back-compat). No cross-deployment
	# allocation tracking — the operator assigns indices deliberately.

	@property
	def gpu_vram_gb(self):
		"""VRAM per pinned GPU, taken as the smallest of them — a mixed box is capped by its
		smallest card. None until the Machine GPU rows carry a VRAM figure."""
		sizes = [row.vram_gb for row in self.gpus or [] if row.vram_gb]
		return min(sizes) if sizes else None

	def _machine(self):
		"""The Machine backing this deployment's Inference Server (or None)."""
		if not self.inference_server:
			return None
		return frappe.db.get_value("Inference Server", self.inference_server, "machine")

	def _machine_gpu(self, machine, gpu_index):
		"""The Machine GPU child row for (machine, CUDA index), or None."""
		rows = frappe.get_all(
			"Machine GPU",
			filters={"parent": machine, "parenttype": "Machine", "gpu_index": gpu_index},
			fields=["name", "gpu_model", "vram_gb"],
			limit=1,
		)
		return rows[0] if rows else None

	def _validate_gpus(self):
		"""Derive tensor_parallel_size, reject duplicate/unknown GPUs, and fill each row's
		display columns from its Machine GPU."""
		seen = set()
		for r in self.gpus or []:
			if r.gpu_index in seen:
				frappe.throw(f"GPU index {r.gpu_index} is listed twice.")
			seen.add(r.gpu_index)

		# Fill the display columns first — the placement check reads vram_gb off these rows.
		# No box yet → the reqd check on inference_server flags it; the split is still checked.
		if machine := self._machine():
			for r in self.gpus or []:
				row = self._machine_gpu(machine, r.gpu_index)
				if not row:
					frappe.throw(f"Machine {machine} has no GPU with CUDA index {r.gpu_index}.")
				r.gpu_model = row.gpu_model
				r.vram_gb = row.vram_gb

		self.reject_claimed_gpus()

		serve = ServeCommand.for_deployment(self)
		if errors := serve.placement_errors:
			frappe.throw("<br>".join(errors))
		self.tensor_parallel_size = serve.tensor_parallel_size

	def reject_claimed_gpus(self):
		"""A GPU backs one engine at a time — two vLLMs on one card split its VRAM and both
		then OOM at a size each thought it had. Read live off the other Active deployments on
		this box rather than a stored flag, so it always matches what's really running.
		Called on save for early feedback, and again at deploy time because a sibling can go
		Active in between (status moves via db.set_value, which skips validate)."""
		declared = {int(r.gpu_index) for r in self.gpus or []}
		if not (declared and self.inference_server):
			return
		siblings = frappe.get_all(
			"Model Deployment",
			filters={
				"inference_server": self.inference_server,
				"name": ["!=", self.name or ""],
				"status": "Active",
			},
			pluck="name",
		)
		if not siblings:
			return
		clashes = {}
		for row in frappe.get_all(
			"Model Deployment GPU",
			filters={"parent": ["in", siblings], "parenttype": "Model Deployment"},
			fields=["parent", "gpu_index"],
		):
			if int(row.gpu_index) in declared:
				clashes.setdefault(row.parent, []).append(int(row.gpu_index))
		if clashes:
			frappe.throw(
				"<br>".join(
					f"GPU {', '.join(str(i) for i in sorted(indices))} on {self.inference_server} "
					f"is already serving Active deployment {deployment}."
					for deployment, indices in clashes.items()
				)
			)

	def _assign_engine_port(self):
		"""Allocate a box-local vLLM port once (multi-tenant box: one port per
		deployment). Lowest free port from ENGINE_PORT_BASE up, scoped to THIS
		Inference Server, skipping ports held by non-freed deployments. Ports released
		by teardown (status Inactive/Terminated + port cleared) are reused. Assigned
		once, then stable — teardown clears it so a later redeploy reallocates."""
		if self.engine_port or not self.inference_server:
			return
		used = {
			p
			for p in frappe.get_all(
				"Model Deployment",
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

	def _derive_engine_url(self):
		"""engine_url is where the gateway forwards — this deployment's OWN vLLM
		instance, i.e. http://<machine public ip>:<engine_port>. No LiteLLM front:
		vLLM 0.24+ serves /v1/messages natively + reports the prefix cache. Derived
		(read-only) from the box IP + the auto-allocated engine_port, never hand-typed.
		Runs AFTER _assign_engine_port and before the mandatory check, so both the
		port and this reqd field are satisfied here."""
		if not self.inference_server:
			return  # no box yet → let the reqd check flag engine_url
		machine_ip = frappe.db.get_value("Inference Server", self.inference_server, "machine_ip")
		if not machine_ip:
			frappe.throw(
				f"Inference Server {self.inference_server} has no machine IP "
				"(set its Machine's public IP) — cannot derive Engine URL."
			)
		port = self.engine_port or ENGINE_PORT_BASE
		self.engine_url = f"http://{machine_ip}:{port}"

	def on_update(self):
		# A model is "published" only while it has a live deployment — keep the
		# parent Model in sync whenever this deployment's status changes.
		if self.has_value_changed("status"):
			from grove.grove.doctype.model.model import sync_published

			sync_published(self.model)

	def on_trash(self):
		# Deleting this deployment may drop the model's last Active placement.
		# Exclude self — the row is still in the DB during on_trash.
		from grove.grove.doctype.model.model import sync_published

		sync_published(self.model, exclude=self.name)

	@frappe.whitelist()
	def setup(self):
		"""Button: (re)serve this deployment's model on its Inference Server via
		the deploy/vllm role (args from the Model launch profile ⊕ this doc), then
		wire the gateway route."""
		frappe.enqueue(
			"grove.grove.doctype.model_deployment.model_deployment.deploy_model",
			queue="long",
			timeout=3600,
			model_deployment=self.name,
		)
		frappe.msgprint(f"Deploying {self.model} on {self.inference_server} — watch its Ansible Plays.")

	@frappe.whitelist()
	def apply_engine_config(self):
		"""Button: re-render this deployment's engine config and restart it to apply edited
		per-box tuning (dtype / gpu_memory_utilization / attention_backend / engine_port /
		env rows). Fast path — skips the heavy weights predownload. Replaces the container,
		so it drops in-flight requests."""
		frappe.enqueue(
			"grove.grove.doctype.model_deployment.model_deployment.reconfigure_deployment",
			queue="long",
			timeout=1200,
			model_deployment=self.name,
		)
		frappe.msgprint(f"Re-rendering engine config on {self.inference_server} — watch its Ansible Plays.")

	@frappe.whitelist()
	def teardown(self):
		"""Button: stop + remove THIS deployment's container, its run script and its key file
		from the box (multi-tenant teardown). Shared weights and the pulled image are left for
		other instances. → Inactive on success."""
		frappe.enqueue(
			"grove.grove.doctype.model_deployment.model_deployment.teardown_deployment",
			queue="long",
			timeout=600,
			model_deployment=self.name,
		)
		frappe.msgprint(f"Tearing down this instance on {self.inference_server} — watch its Ansible Plays.")

	@property
	def container_name(self):
		"""This deployment's container on its box. One instance per Model Deployment, so a box
		can run many at once."""
		return f"vllm-{_instance_slug(self.name)}"

	@property
	def machine(self):
		"""The box this deployment's engine runs on, via its Inference Server."""
		machine = frappe.db.get_value("Inference Server", self.inference_server, "machine")
		if not machine:
			frappe.throw(f"{self.inference_server} has no Machine to reach.")
		return frappe.get_doc("Machine", machine)

	@frappe.whitelist()
	def get_engine_logs(self, lines: int = 200):
		"""Button: this engine container's own log, read off its box. Where a failed deploy
		explains itself: the Ansible Play only says the health gate timed out, the engine says
		why it never came up. Read-only and not stored on the doc; a crash-looping container's
		log is the whole point and it changes every few seconds."""
		return self.machine.run_command(
			["docker", "logs", "--tail", str(_log_lines(lines)), self.container_name]
		)

	@frappe.whitelist()
	def stream_engine_logs(self):
		"""Button: follow this engine container's log off its box (`docker logs --follow` over
		SSH), relayed to this form for as long as it keeps pinging keep_streaming. Deduplicated
		per deployment, so a second Start — after a page reload, say — does not double up the
		stream."""
		from grove import log_relay

		self.machine  # resolved here so a missing box fails on the button, not in a worker
		log_relay.keep_alive(self.doctype, self.name)
		frappe.enqueue(
			"grove.grove.doctype.model_deployment.model_deployment.stream_engine_logs",
			queue="long", timeout=1800, job_id=f"md-logs-{self.name}", deduplicate=True,
			model_deployment=self.name,
		)

	@frappe.whitelist()
	def stop_engine_logs(self):
		"""Tell the streaming job to finish — it checks between publishes."""
		from grove import log_relay

		log_relay.end(self.doctype, self.name)


def stream_engine_logs(model_deployment):
	"""Job: follow the deployment's container log over SSH and relay it to its form."""
	from grove import log_relay

	md = frappe.get_doc("Model Deployment", model_deployment)
	command = ["docker", "logs", "--follow", "--tail", str(_log_lines(md.log_lines)), md.container_name]
	log_relay.relay(md.machine.stream_command(command), md.doctype, md.name)


def _log_lines(lines):
	"""Backfill size, clamped — `docker logs --tail` will happily read a whole disk."""
	return max(1, min(int(lines or 200), 5000))


def _instance_slug(md_name):
	"""Per-deployment slug, safe as a container name → vllm-<slug>, and the key file beside
	it. One instance per Model Deployment so a box can run many at once."""
	return re.sub(r"[^a-z0-9._-]", "-", md_name.lower())


def _engine_env(md, hf_token):
	"""Env vars for the engine container: what Grove derives from the deployment first, the
	operator's own rows layered on top (same precedence the Pod path uses). VLLM_API_KEY is
	NOT here — the role resolves it on the box, so the env-file template there adds it."""
	env = {}
	if hf_token:
		env["HF_TOKEN"] = hf_token
	if md.allow_long_max_model_len:
		env["VLLM_ALLOW_LONG_MAX_MODEL_LEN"] = "1"
	env.update({row.key: row.value or "" for row in md.env or []})
	return env


def _vllm_extravars(md, m, inf, key):
	"""Assemble the vLLM Ansible extra-vars from the Model launch profile (m) ⊕ this
	deployment (md) ⊕ its box (inf) ⊕ the internal key. Shared by deploy_model (full serve)
	and reconfigure_deployment (config-only) so the two paths can never drift. The `vllm serve`
	flags come from ServeCommand — the same builder the Pod path uses; the run script on the
	box only renders them."""
	serve = ServeCommand.for_deployment(md)

	# GPU pinning: the deployment names CUDA indices on its box (md.gpus). N GPUs →
	# tensor-parallel across exactly those, with CUDA_VISIBLE_DEVICES so vLLM only
	# sees them. No rows → single-GPU, unpinned (whatever the box exposes).
	gpu_indexes = sorted(int(r.gpu_index) for r in (md.gpus or []))

	hf_token = frappe.conf.get("hf_token", "") if m.gated else ""
	vllm_home = inf.data_path

	extravars = {
		"vllm_model": m.hf_repo,
		"vllm_served_name": " ".join([md.model, *serve.aliases]),
		"vllm_serve_args": serve.args,
		# One container + port + key file per deployment (multi-tenant box). Slug from the
		# MD name → container vllm-<instance> and its run script beside the key.
		"vllm_instance": _instance_slug(md.name),
		"vllm_port": serve.port,
		"vllm_api_key": key,
		"vllm_cuda_visible_devices": ",".join(str(i) for i in gpu_indexes),
		"vllm_env": _engine_env(md, hf_token),
		"vllm_hf_token": hf_token,
		# Keep the weights/caches on the mounted data volume (root is tiny / ephemeral).
		"vllm_home": vllm_home,
		"vllm_hf_home": f"{vllm_home}/hf",
		"vllm_cache_dir": f"{vllm_home}/cache",
		"vllm_predownload_model": True,
	}
	# The image is what the box serves from: the role pulls it and Docker owns the container.
	# Credentials only exist for a private registry; a public pull skips the login task.
	image = frappe.get_cached_doc("Engine Image", md.engine_image)
	extravars["vllm_image"] = image.full_image
	if credentials := image.registry_credentials:
		extravars["vllm_registry_host"] = image.registry_host
		extravars["vllm_registry_username"], extravars["vllm_registry_token"] = credentials
	return extravars


def deploy_model(model_deployment):
	"""Serve a Model Deployment on its Inference Server via the deploy/vllm role.
	Every vLLM arg is assembled from the Model launch profile ⊕ this deployment
	(§8E) and passed as Ansible extra-vars — the DocTypes are the source of truth,
	not a hand-written group_vars. On success → Active + push the routing table."""
	md = frappe.get_doc("Model Deployment", model_deployment)
	if not md.inference_server:
		frappe.throw(f"Model Deployment {md.name} has no Inference Server")
	m = frappe.get_doc("Model", md.model)
	inf = frappe.get_doc("Inference Server", md.inference_server)
	if not inf.is_provisioned:
		frappe.throw(
			f"Inference Server {inf.name} is not provisioned — run its Setup "
			"(host bootstrap) before deploying a model onto it."
		)
	# Re-checked here, not just on save: another deployment can have gone Active in the
	# meantime, and status moves by db.set_value, which never runs validate.
	md.reject_claimed_gpus()

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

	frappe.db.set_value("Model Deployment", md.name, "status", "Provisioning")
	frappe.db.commit()

	project_dir = ansible_project_dir("vllm")
	ansible = Ansible(project_root=project_dir)
	play_name, rc = ansible.run_playbook(
		playbook_name="serve.yml",
		server_type="Inference Server",
		server_name=inf.name,
		machine_name=inf.machine,
		extravars=extravars,
		reference_doctype="Model Deployment",
		reference_docname=md.name,
	)

	frappe.db.set_value("Model Deployment", md.name, "status", "Active" if rc == 0 else "Broken")
	# db.set_value skips the controller on_update — recompute the model's
	# published flag (Active deployment → publishable, Broken → maybe unpublish).
	from grove.grove.doctype.model.model import sync_published

	sync_published(md.model)
	frappe.db.commit()
	if rc == 0:
		from grove import gateway_sync

		# Routes are global (no per-deployment proxy) → push to every Active proxy.
		gateway_sync.full_sync(trigger="Provision")
	return play_name, rc


def reconfigure_deployment(model_deployment):
	"""Re-render the engine config and restart it for an already-served
	deployment, applying edited per-box tuning (dtype / gpu_memory_utilization /
	attention_backend / engine_port / env rows) WITHOUT the heavy weights predownload.
	Runs serve.yml with skip_tags=["heavy"] — every config/replace/health task still
	runs (identical to a full serve minus the download), just faster. The image pull is
	not heavy, so a moved tag lands here too. Assumes the box is provisioned and the
	weights are already in its shared cache.
	Restarts the engine → drops in-flight requests, so it's button-triggered, not
	automatic."""
	md = frappe.get_doc("Model Deployment", model_deployment)
	if not md.inference_server:
		frappe.throw(f"Model Deployment {md.name} has no Inference Server")
	m = frappe.get_doc("Model", md.model)
	inf = frappe.get_doc("Inference Server", md.inference_server)
	if not inf.is_provisioned:
		frappe.throw(f"Inference Server {inf.name} is not provisioned — deploy the model first.")

	key = md.get_password("internal_api_key", raise_exception=False)
	if not key:
		frappe.throw(
			f"Model Deployment {md.name} has no internal key — run a full Deploy "
			"before reconfiguring."
		)

	extravars = _vllm_extravars(md, m, inf, key)

	frappe.db.set_value("Model Deployment", md.name, "status", "Provisioning")
	frappe.db.commit()

	project_dir = ansible_project_dir("vllm")
	ansible = Ansible(project_root=project_dir)
	play_name, rc = ansible.run_playbook(
		playbook_name="serve.yml",
		server_type="Inference Server",
		server_name=inf.name,
		machine_name=inf.machine,
		extravars=extravars,
		skip_tags=["heavy"],
		reference_doctype="Model Deployment",
		reference_docname=md.name,
	)

	frappe.db.set_value("Model Deployment", md.name, "status", "Active" if rc == 0 else "Broken")
	frappe.db.commit()
	return play_name, rc


def teardown_deployment(model_deployment):
	"""Stop + remove ONE deployment's container, the run script and env file that would
	restart it, and its key file (multi-tenant teardown). Leaves the box-shared
	weights/compile caches and the pulled image intact — other instances may use them.
	On success → Inactive."""
	md = frappe.get_doc("Model Deployment", model_deployment)
	inf = frappe.get_doc("Inference Server", md.inference_server)

	project_dir = ansible_project_dir("vllm")
	ansible = Ansible(project_root=project_dir)
	play_name, rc = ansible.run_playbook(
		playbook_name="teardown.yml",
		server_type="Inference Server",
		server_name=inf.name,
		machine_name=inf.machine,
		extravars={
			"vllm_instance": _instance_slug(md.name),
			"vllm_home": inf.data_path,
		},
		reference_doctype="Model Deployment",
		reference_docname=md.name,
	)

	if rc == 0:
		# Release the box-local port (0 = free → reallocated on a later redeploy;
		# the Int column is NOT NULL, so 0 not None).
		frappe.db.set_value(
			"Model Deployment", md.name, {"status": "Inactive", "engine_port": 0}
		)
		from grove.grove.doctype.model.model import sync_published

		sync_published(md.model)
		frappe.db.commit()
	return play_name, rc
