# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import secrets

import frappe
from frappe.model.document import Document

from grove.serve_command import ServeCommand

# Default port pool seeded on a fresh Pod: SSH + a pool of vLLM engine ports (the provider
# can't hot-add ports, so open them at spawn).
_ENGINE_PORT = 8080


class Pod(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.pod_env.pod_env import PodEnv
		from grove.grove.doctype.pod_port.pod_port import PodPort

		aliases: DF.SmallText | None
		allow_long_max_model_len: DF.Check
		api_key: DF.Password | None
		attention_backend: DF.Literal["auto", "FLASH_ATTN", "XFORMERS", "FLASHINFER"]
		cloud_provider: DF.Link
		container_disk_gb: DF.Int
		dtype: DF.Literal["auto", "bfloat16", "float16", "float32"]
		engine_image: DF.Link | None
		engine_url: DF.Data | None
		env: DF.Table[PodEnv]
		extra_serve_args: DF.SmallText | None
		gpu_count: DF.Int
		gpu_memory_utilization: DF.Float
		gpu_type_id: DF.Data | None
		image_name: DF.Data | None
		max_model_len: DF.Int
		model: DF.Link | None
		pipeline_parallel_size: DF.Int
		pod_id: DF.Data | None
		ports: DF.Table[PodPort]
		public_ip: DF.Data | None
		scheduling_policy: DF.Literal["fcfs", "priority"]
		serve_command: DF.Code | None
		serve_port: DF.Int
		ssh_port: DF.Int
		ssh_user: DF.Data | None
		startup_command: DF.SmallText | None
		status: DF.Literal["Pending", "Provisioning", "Loading", "Running", "Stopped", "Terminated"]
		template_id: DF.Data | None
		volume_in_gb: DF.Int
		volume_mount_path: DF.Data | None
	# end: auto-generated types

	"""A cloud GPU pod (e.g. RunPod), modelled on the provider's pod-create request: GPU
	list, port pool, image/template, startup command, volume, SSH, env.

	Two modes:
	- Machine-backed (Model blank): base image + Ansible process runtime. Backs a Machine
	  1:1; the provisioner cascades flat mirrors (port_map / GPUs / IP) onto the Machine and
	  Inference Server / Model Deployment / Ansible read the Machine.
	- Standalone serving (Model set): a vLLM image serves the linked Model directly. The
	  vLLM params below are translated into serve_command (the container's dockerStartCmd);
	  the pod IS the deployment. Operate it from its own Spawn / Sync / Restart / Terminate
	  buttons. Model-intrinsic flags come from the Model; the Pod holds per-box tuning."""

	def before_insert(self):
		# Seed a sensible port pool: 22 (SSH — Ansible/ops connect over it) + a pool of vLLM
		# engine ports. Opened at spawn since the provider can't hot-add ports later.
		if not self.ports:
			self.append("ports", {"internal_port": 22, "protocol": "tcp"})
			self.append("ports", {"internal_port": _ENGINE_PORT, "protocol": "tcp"})

	def validate(self):
		if not self.template_id and not (self.engine_image or self.image_name):
			frappe.throw("Set an Engine Image or a manual Image — the pod needs one to spawn.")
		if self.model:
			if not self.api_key:
				self.api_key = secrets.token_hex(24)  # vLLM --api-key via VLLM_API_KEY env
			serve_port = int(self.serve_port or _ENGINE_PORT)
			if not any(int(p.internal_port) == serve_port for p in self.ports or []):
				frappe.throw(
					f"Serve Port {serve_port} is not in the Ports table — add it so it's opened at spawn."
				)
			serve = ServeCommand.for_pod(self)
			if errors := serve.placement_errors:
				frappe.throw("<br>".join(errors))
			self.serve_command = serve.command
		else:
			self.serve_command = ""

	@property
	def gpu_vram_gb(self):
		"""VRAM per GPU for this pod's GPU type, from the Cloud Provider's cached type list.
		None until that cache is fetched (or for a type the provider no longer lists)."""
		if not (self.cloud_provider and self.gpu_type_id):
			return None
		cached = frappe.db.get_value("Cloud Provider", self.cloud_provider, "gpu_types")
		for gpu_type in json.loads(cached or "[]"):
			if gpu_type.get("id") == self.gpu_type_id:
				return gpu_type.get("memoryInGb")
		return None

	@property
	def resolved_image(self):
		"""Image ref to spawn from: the Engine Image (registry host included) if linked, else
		the manual field. None when a Template supplies the image."""
		if self.engine_image:
			return frappe.get_cached_doc("Engine Image", self.engine_image).full_image
		return self.image_name or None

	# ── Standalone lifecycle (Model-serving pods; also usable for machine-less base pods) ──
	@frappe.whitelist()
	def spawn(self):
		"""Spawn this pod on its provider (long job)."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.spawn_pod_doc",
			queue="long", timeout=1800, pod_name=self.name,
		)
		frappe.msgprint(f"Spawning pod {self.name} on {self.cloud_provider}.")

	@frappe.whitelist()
	def sync(self):
		"""Pull the pod's current state (IP / external ports / status) off the provider and
		update this Pod (and its Machine, if linked). Runs inline for immediate feedback."""
		from grove.cloud_provider.provisioner import sync_pod

		res = sync_pod(self.name)
		frappe.msgprint(f"Synced pod {self.name}: {res['status']}.")
		return res

	@frappe.whitelist()
	def stop(self):
		"""Free the GPU but keep the pod and its volume (weights survive); Start resumes it."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.stop_pod",
			queue="short", timeout=600, pod_name=self.name,
		)
		frappe.msgprint(f"Stopping pod {self.name} — volume kept, GPU released.")

	@frappe.whitelist()
	def start(self):
		"""Resume a stopped pod and re-read its endpoints (the provider re-maps ports)."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.start_pod",
			queue="long", timeout=1800, pod_name=self.name,
		)
		frappe.msgprint(f"Starting pod {self.name} — ports will change, then sync.")

	@frappe.whitelist()
	def restart(self):
		"""Apply edited config in place: the provider pod is updated with the current
		serve_command and resets, keeping its id and volume (so weights are not re-downloaded),
		then endpoints are re-synced. Refused for an edit the provider can't apply to a live pod
		— an edited GPU shape needs Terminate + Spawn. Long job."""
		from grove.cloud_provider.provisioner import PodProvisioner

		# Throws here, so the operator sees why rather than getting a dead background job.
		PodProvisioner(self).validate_restart()
		frappe.enqueue(
			"grove.cloud_provider.provisioner.restart_pod",
			queue="long", timeout=1800, pod_name=self.name,
		)
		frappe.msgprint(f"Restarting pod {self.name} to apply config, then sync.")

	@frappe.whitelist()
	def terminate(self):
		"""Terminate the provider pod (frees GPU/disk/billing) and mark this Pod Terminated."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.terminate_pod_doc",
			queue="long", timeout=600, pod_name=self.name,
		)
		frappe.msgprint(f"Terminating pod {self.name}.")
