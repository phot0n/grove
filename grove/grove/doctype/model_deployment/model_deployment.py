# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import os
import re
import secrets

import frappe
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.provision import _app_grove_root


ENGINE_PORT_BASE = 8080
# Statuses whose deployment no longer holds a systemd unit on the box → its port is
# free to reuse. Everything else (Draft/Provisioning/Active/Broken) reserves its port.
_PORT_FREE_STATUSES = ("Inactive", "Terminated")


class ModelDeployment(Document):
	# Routes are not dirty-gated: grove.gateway_sync.sync_dirty pushes the full
	# route table for every deployment each run (idempotent), so no on_update
	# hook is needed for routing.

	def validate(self):
		self._assign_engine_port()
		self._derive_engine_url()
		self._validate_gpus()

	# ── GPU allocation ────────────────────────────────────────────────────────
	# GPUs live as child rows on the box's Machine (Machine GPU). A deployment
	# names the CUDA indices it wants (self.gpus). validate() checks they exist +
	# aren't held by another deployment and fills the display columns; the actual
	# Free→Allocated flip happens at deploy time (allocate_gpus) and is reversed on
	# teardown/trash (free_gpus). No GPU rows → single-GPU, unpinned (back-compat).

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
			fields=["name", "gpu_model", "vram_gb", "status", "allocated_to"],
			limit=1,
		)
		return rows[0] if rows else None

	def _validate_gpus(self):
		"""Derive tensor_parallel_size, reject duplicate/unknown/busy GPUs, and
		fill each row's display columns from its Machine GPU."""
		seen = set()
		for r in self.gpus or []:
			if r.gpu_index in seen:
				frappe.throw(f"GPU index {r.gpu_index} is listed twice.")
			seen.add(r.gpu_index)
		self.tensor_parallel_size = len(self.gpus or []) or 1

		machine = self._machine()
		if not machine:
			return  # no box yet → reqd check on inference_server will flag it
		for r in self.gpus or []:
			row = self._machine_gpu(machine, r.gpu_index)
			if not row:
				frappe.throw(f"Machine {machine} has no GPU with CUDA index {r.gpu_index}.")
			if row.allocated_to and row.allocated_to != self.name:
				frappe.throw(
					f"GPU {r.gpu_index} on {machine} is already allocated to {row.allocated_to}."
				)
			r.gpu_model = row.gpu_model
			r.vram_gb = row.vram_gb

	def allocate_gpus(self):
		"""Mark this deployment's declared GPUs Allocated on their Machine, and free
		any it previously held but no longer names (GPU set changed). Idempotent —
		safe to call on every (re)deploy. Called from provision at serve time."""
		machine = self._machine()
		if not machine:
			return
		declared = {int(r.gpu_index) for r in self.gpus or []}
		# Release GPUs we hold but no longer want.
		for held in frappe.get_all(
			"Machine GPU", filters={"allocated_to": self.name}, fields=["name", "gpu_index"]
		):
			if int(held.gpu_index) not in declared:
				frappe.db.set_value(
					"Machine GPU", held.name, {"status": "Free", "allocated_to": ""},
					update_modified=False,
				)
		# Claim the declared GPUs.
		for idx in declared:
			row = self._machine_gpu(machine, idx)
			if not row:
				frappe.throw(f"Machine {machine} has no GPU with CUDA index {idx}.")
			if row.allocated_to and row.allocated_to != self.name:
				frappe.throw(
					f"GPU {idx} on {machine} is already allocated to {row.allocated_to}."
				)
			frappe.db.set_value(
				"Machine GPU", row.name, {"status": "Allocated", "allocated_to": self.name},
				update_modified=False,
			)

	def free_gpus(self):
		"""Release every Machine GPU held by this deployment. Called on teardown/trash."""
		for name in frappe.get_all(
			"Machine GPU", filters={"allocated_to": self.name}, pluck="name"
		):
			frappe.db.set_value(
				"Machine GPU", name, {"status": "Free", "allocated_to": ""},
				update_modified=False,
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
		# Cloud pod: reachable engine ports are only those pre-opened at spawn (the
		# Machine's port_map) — RunPod can't hot-add ports. Claim the lowest free one from
		# that pool; full pod → throw. On-prem: lowest free from the base, unbounded.
		machine = self._machine()
		prov, port_map = (
			frappe.db.get_value("Machine", machine, ["cloud_provider", "port_map"])
			if machine
			else (None, None)
		)
		if prov:
			if port_map:
				pool = sorted(int(p) for p in json.loads(port_map) if int(p) != 22)
			else:  # pod not provisioned yet — fall back to the declared pool shape
				from grove.cloud_provider.provisioner import ENGINE_PORT_POOL_SIZE

				pool = [ENGINE_PORT_BASE + i for i in range(ENGINE_PORT_POOL_SIZE)]
			for port in pool:
				if port not in used:
					self.engine_port = port
					return
			frappe.throw(f"Pod {machine} is full — all {len(pool)} model slots are in use.")
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
		inf = frappe.db.get_value(
			"Inference Server", self.inference_server, ["machine", "machine_ip"], as_dict=True
		)
		if not inf or not inf.machine_ip:
			frappe.throw(
				f"Inference Server {self.inference_server} has no machine IP "
				"(set its Machine's public IP) — cannot derive Engine URL."
			)
		port = self.engine_port or ENGINE_PORT_BASE
		# Cloud pod → the vLLM port is NAT'd to a random external port; look it up in the
		# Machine's port_map. On-prem → the internal port is directly reachable.
		prov, port_map = frappe.db.get_value("Machine", inf.machine, ["cloud_provider", "port_map"])
		if prov:
			ext = json.loads(port_map or "{}").get(str(port))
			if not ext:
				frappe.throw(
					f"Engine port {port} is not exposed on pod {inf.machine} — "
					"widen the port pool or re-provision."
				)
			port = ext
		self.engine_url = f"http://{inf.machine_ip}:{port}"

	def on_update(self):
		# A model is "published" only while it has a live deployment — keep the
		# parent Model in sync whenever this deployment's status changes.
		if self.has_value_changed("status"):
			from grove.grove.doctype.model.model import sync_published

			sync_published(self.model)

	def on_trash(self):
		# Release any GPUs this deployment still holds (teardown may not have run).
		self.free_gpus()
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
		"""Button: re-render + restart the vLLM systemd unit to apply edited
		per-box tuning (dtype / gpu_memory_utilization / attention_backend / engine_port)
		without a full re-install. Fast path — skips the heavy install/pip/predownload
		tasks. Restarts the engine, so it drops in-flight requests."""
		frappe.enqueue(
			"grove.grove.doctype.model_deployment.model_deployment.reconfigure_deployment",
			queue="long",
			timeout=1200,
			model_deployment=self.name,
		)
		frappe.msgprint(f"Re-rendering engine config on {self.inference_server} — watch its Ansible Plays.")

	@frappe.whitelist()
	def teardown(self):
		"""Button: stop + remove THIS deployment's vLLM systemd unit + key file from
		its box (multi-tenant teardown). Shared per-version venv + weights are left
		for other instances. Status → Inactive on success."""
		frappe.enqueue(
			"grove.grove.doctype.model_deployment.model_deployment.teardown_deployment",
			queue="long",
			timeout=600,
			model_deployment=self.name,
		)
		frappe.msgprint(f"Tearing down this instance on {self.inference_server} — watch its Ansible Plays.")


def _instance_slug(md_name):
	"""Systemd-safe per-deployment slug → unit vllm-<slug>.service + key file. One
	instance per Model Deployment so a box can run many concurrently."""
	return re.sub(r"[^a-z0-9._-]", "-", md_name.lower())


def _vllm_home(inference_server):
	"""Where vLLM keeps venvs/weights/keys/caches. On a cloud pod the container root is
	ephemeral (wiped on restart) — use the persistent volume; on-prem uses local /opt."""
	from grove.cloud_provider.provisioner import VOLUME_MOUNT

	machine = inference_server and frappe.db.get_value("Inference Server", inference_server, "machine")
	is_cloud = bool(machine and frappe.db.get_value("Machine", machine, "cloud_provider"))
	return f"{VOLUME_MOUNT}/vllm" if is_cloud else "/opt/vllm"


def _vllm_extravars(md, m, key):
	"""Assemble the vLLM Ansible extra-vars from the Model launch profile (m) ⊕ this
	deployment (md) ⊕ the internal key. Shared by deploy_model (full serve) and
	reconfigure_deployment (unit-only) so the two paths can never drift."""
	# Embedding/pooling models serve /v1/embeddings, not chat — force --task embed
	# and drop the chat-only tool/reasoning flags (meaningless for pooling).
	is_embed = bool(m.is_embedding)

	# Extra `vllm serve` flags. dtype is a per-box tuning knob → from the
	# deployment (md); quantization/modality are model-intrinsic → from the Model (m).
	extra = []
	if is_embed:
		extra += ["--task", "embed"]
	if (md.dtype or "auto") != "auto":
		extra += ["--dtype", md.dtype]
	if m.quantization:
		extra += ["--quantization", m.quantization]
	if m.modality == "text":
		extra.append("--language-model-only")
	if m.extra_serve_args:
		extra += m.extra_serve_args.split()

	aliases = (m.aliases or "").replace(",", " ").split()
	served = " ".join([md.model, *aliases])

	# GPU pinning: the deployment names CUDA indices on its box (md.gpus). N GPUs →
	# tensor-parallel across exactly those, with CUDA_VISIBLE_DEVICES so vLLM only
	# sees them. No rows → single-GPU, unpinned (whatever the box exposes).
	gpu_indexes = sorted(int(r.gpu_index) for r in (md.gpus or []))

	vllm_home = _vllm_home(md.inference_server)

	extravars = {
		"vllm_model": m.hf_repo,
		"vllm_served_name": served,
		# One systemd unit + port + key file + venv per deployment (multi-tenant box).
		# Slug from the MD name → unit vllm-<instance>.service, venv <instance>_venv.
		"vllm_instance": _instance_slug(md.name),
		"vllm_version": md.engine_version or "",  # "" = latest; else pip vllm==<ver>
		"vllm_port": md.engine_port or 8080,
		"vllm_api_key": key,
		"vllm_gpu_memory_utilization": md.gpu_memory_utilization or 0.9,
		"vllm_tensor_parallel_size": len(gpu_indexes) or 1,
		"vllm_cuda_visible_devices": ",".join(str(i) for i in gpu_indexes),
		"vllm_max_model_len": m.max_model_len or 8192,
		"vllm_attention_backend": md.attention_backend or "auto",
		"vllm_use_flashinfer_sampler": "0",
		"vllm_enable_prefix_caching": bool(m.enable_prefix_caching),
		# Chat-only knobs → off for embedding models.
		"vllm_enable_auto_tool_choice": bool(m.enable_auto_tool_choice) and not is_embed,
		"vllm_tool_call_parser": "" if is_embed else (m.tool_call_parser or ""),
		"vllm_reasoning_parser": "" if is_embed else ((m.reasoning_parser or "") if m.thinking else ""),
		"vllm_extra_serve_args": extra,
		# Keep the venv/weights/caches on the mounted data volume (root is tiny / ephemeral).
		"vllm_home": vllm_home,
		"vllm_hf_home": f"{vllm_home}/hf",
		"vllm_cache_dir": f"{vllm_home}/cache",
		"vllm_predownload_model": True,
	}
	if m.gated:
		extravars["vllm_hf_token"] = frappe.conf.get("hf_token", "")
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

	# Grove owns the internal key: generate once and serve with it, so the gateway
	# (which stores it as the deployment's internal_api_key) always matches.
	key = md.get_password("internal_api_key", raise_exception=False)
	if not key:
		key = secrets.token_hex(24)
		md.internal_api_key = key
		md.save(ignore_permissions=True)
		frappe.db.commit()

	extravars = _vllm_extravars(md, m, key)

	# Reserve this deployment's GPUs on the box before serving (Free→Allocated);
	# raises if any is already held by another deployment. Freed on teardown/trash.
	md.allocate_gpus()

	frappe.db.set_value("Model Deployment", md.name, "status", "Provisioning")
	frappe.db.commit()

	project_dir = os.path.join(_app_grove_root(), "deploy", "vllm", "ansible")
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
	"""Re-render + restart the vLLM systemd unit for an already-served
	deployment, applying edited per-box tuning (dtype / gpu_memory_utilization /
	attention_backend / engine_port) WITHOUT the heavy install/pip/predownload steps.
	Runs serve.yml with skip_tags=["heavy"] — every config/unit/restart/health task
	still runs (identical to a full serve minus install), just faster. Assumes the box
	is provisioned and the model was served at least once (venv + weights present).
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

	extravars = _vllm_extravars(md, m, key)

	# Re-sync GPU allocation in case the deployment's GPU set was edited; also
	# releases any GPU it dropped. Raises on conflict with another deployment.
	md.allocate_gpus()

	frappe.db.set_value("Model Deployment", md.name, "status", "Provisioning")
	frappe.db.commit()

	project_dir = os.path.join(_app_grove_root(), "deploy", "vllm", "ansible")
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
	"""Stop + remove ONE deployment's vLLM systemd unit + key file + its (exclusive)
	per-deployment venv from its box (multi-tenant teardown). Leaves the box-shared
	weights/compile caches intact — other instances may use them. On success →
	Inactive."""
	md = frappe.get_doc("Model Deployment", model_deployment)
	inf = frappe.get_doc("Inference Server", md.inference_server)

	project_dir = os.path.join(_app_grove_root(), "deploy", "vllm", "ansible")
	ansible = Ansible(project_root=project_dir)
	play_name, rc = ansible.run_playbook(
		playbook_name="teardown.yml",
		server_type="Inference Server",
		server_name=inf.name,
		machine_name=inf.machine,
		extravars={"vllm_instance": _instance_slug(md.name), "vllm_home": _vllm_home(md.inference_server)},
		reference_doctype="Model Deployment",
		reference_docname=md.name,
	)

	if rc == 0:
		# Release the box-local port (0 = free → reallocated on a later redeploy;
		# the Int column is NOT NULL, so 0 not None) and this deployment's GPUs.
		md.free_gpus()
		frappe.db.set_value(
			"Model Deployment", md.name, {"status": "Inactive", "engine_port": 0}
		)
		from grove.grove.doctype.model.model import sync_published

		sync_published(md.model)
		frappe.db.commit()
	return play_name, rc
