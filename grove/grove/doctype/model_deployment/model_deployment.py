# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re
import secrets

import frappe
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.utils import ansible_project_dir
from grove.serve_command import ServeCommand


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

	# ── GPU pinning ───────────────────────────────────────────────────────────
	# GPUs live as child rows on the box's Machine (Machine GPU). A deployment names the
	# CUDA indices it wants (self.gpus); validate() checks they exist and fills the display
	# columns. No GPU rows → single-GPU, unpinned (back-compat). No cross-deployment
	# allocation tracking — the operator assigns indices deliberately.

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
		self.tensor_parallel_size = len(self.gpus or []) or 1

		machine = self._machine()
		if not machine:
			return  # no box yet → reqd check on inference_server will flag it
		for r in self.gpus or []:
			row = self._machine_gpu(machine, r.gpu_index)
			if not row:
				frappe.throw(f"Machine {machine} has no GPU with CUDA index {r.gpu_index}.")
			r.gpu_model = row.gpu_model
			r.vram_gb = row.vram_gb

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


# Where vLLM keeps venvs/weights/keys/caches on the box.
VLLM_HOME = "/opt/vllm"


def _vllm_extravars(md, m, key):
	"""Assemble the vLLM Ansible extra-vars from the Model launch profile (m) ⊕ this
	deployment (md) ⊕ the internal key. Shared by deploy_model (full serve) and
	reconfigure_deployment (unit-only) so the two paths can never drift. The `vllm serve`
	flags come from ServeCommand — the same builder the Pod (container) placement uses; the
	unit template only renders them."""
	serve = ServeCommand.for_deployment(md)

	# GPU pinning: the deployment names CUDA indices on its box (md.gpus). N GPUs →
	# tensor-parallel across exactly those, with CUDA_VISIBLE_DEVICES so vLLM only
	# sees them. No rows → single-GPU, unpinned (whatever the box exposes).
	gpu_indexes = sorted(int(r.gpu_index) for r in (md.gpus or []))

	vllm_home = VLLM_HOME

	extravars = {
		"vllm_model": m.hf_repo,
		"vllm_served_name": " ".join([md.model, *serve.aliases]),
		"vllm_serve_args": serve.args,
		# One systemd unit + port + key file + venv per deployment (multi-tenant box).
		# Slug from the MD name → unit vllm-<instance>.service, venv <instance>_venv.
		"vllm_instance": _instance_slug(md.name),
		"vllm_version": md.engine_version or "",  # "" = latest; else pip vllm==<ver>
		"vllm_port": serve.port,
		"vllm_api_key": key,
		"vllm_cuda_visible_devices": ",".join(str(i) for i in gpu_indexes),
		"vllm_attention_backend": md.attention_backend or "auto",
		"vllm_use_flashinfer_sampler": "0",
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
	"""Stop + remove ONE deployment's vLLM systemd unit + key file + its (exclusive)
	per-deployment venv from its box (multi-tenant teardown). Leaves the box-shared
	weights/compile caches intact — other instances may use them. On success →
	Inactive."""
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
			"vllm_home": VLLM_HOME,
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
