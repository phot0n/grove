# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import secrets

import frappe
from frappe.model.document import Document

from grove.grove.doctype.engine_image.engine_image import engine_tuning
from grove.grove.doctype.model.model import launch_config
from grove.serving.base import build_engine

# Seeded on a fresh Pod: SSH + the engine port. The provider cannot hot-add ports.
_ENGINE_PORT = 8080


class Pod(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.pod_env.pod_env import PodEnv
		from grove.grove.doctype.pod_port.pod_port import PodPort

		allow_long_max_model_len: DF.Check
		api_key: DF.Password | None
		attention_backend: DF.Literal["auto", "FLASH_ATTN", "XFORMERS", "FLASHINFER"]
		cloud_provider: DF.Link
		container_disk_gb: DF.Int
		engine_image: DF.Link
		engine_kind: DF.Data | None
		engine_url: DF.Data | None
		env: DF.Table[PodEnv]
		extra_serve_args: DF.SmallText | None
		gpu_count: DF.Int
		gpu_memory_utilization: DF.Float
		gpu_type_id: DF.Data | None
		health_path: DF.Data | None
		kv_cache_dtype: DF.Literal["auto", "fp8"]
		max_model_len: DF.Data | None
		max_num_seqs: DF.Int
		model: DF.Link
		monitoring_agent: DF.Link | None
		pipeline_parallel_size: DF.Int
		pod_id: DF.Data | None
		ports: DF.Table[PodPort]
		provider_type: DF.Data | None
		public_ip: DF.Data | None
		serve_command: DF.Code | None
		serve_port: DF.Int
		ssh_port: DF.Int
		ssh_user: DF.Data | None
		startup_command: DF.SmallText | None
		provision_at: DF.Time | None
		provision_attempts: DF.Int
		provision_retries: DF.Int
		status: DF.Literal["Pending", "Provisioning", "Loading", "Running", "Stopped", "Terminated"]
		volume_in_gb: DF.Int
		scheduled_for: DF.Date | None
		terminate_at: DF.Time | None
		volume_mount_path: DF.Data | None
	# end: auto-generated types

	"""A cloud GPU pod, modelled on the provider's pod-create request. The pod IS the deployment:
	it serves its Model directly, with no Model Replica behind it.

	The image decides how it is started. A vllm-kind image takes `vllm serve` arguments, derived
	here into serve_command; a custom-kind image gets only its own entrypoint plus
	startup_command."""

	@property
	def engine(self):
		"""The kind and warmup off its Engine Image, the Model's launch config read live, and this
		pod's own tuning."""
		kind, image_tuning = engine_tuning(self.engine_image)
		return build_engine(
			kind,
			self.model,
			launch_config(self.model),
			port=self.serve_port,
			gpu_count=self.gpu_count,
			pipeline_parallel_size=self.pipeline_parallel_size,
			gpu_vram_gb=self.gpu_vram_gb,
			kv_cache_dtype=self.kv_cache_dtype,
			gpu_memory_utilization=self.gpu_memory_utilization,
			max_model_len=self.max_model_len,
			max_num_seqs=self.max_num_seqs,
			attention_backend=self.attention_backend,
			allow_long_max_model_len=self.allow_long_max_model_len,
			extra_serve_args=self.extra_serve_args,
			startup_command=self.startup_command,
			**image_tuning,
		)

	def before_insert(self):
		# The engine is exposed as http so it rides the provider's HTTPS proxy and needs no
		# certificate of its own. SSH has to stay direct tcp.
		if not self.ports:
			self.append("ports", {"internal_port": 22, "protocol": "tcp"})
			self.append("ports", {"internal_port": _ENGINE_PORT, "protocol": "http"})

	def validate(self):
		# The health gate polls the serve port and the gateway route is built from it, so a port
		# the provider never opened leaves the pod Loading forever with a blank endpoint.
		serve_port = int(self.serve_port or _ENGINE_PORT)
		if not any(int(p.internal_port) == serve_port for p in self.ports or []):
			frappe.throw(
				f"Serve Port {serve_port} is not in the Ports table — add it so it's opened at spawn."
			)
		engine = self.engine
		# The gateway ships this as the route's internal key, so minting one for an image that
		# enforces nothing would send a bearer to something that never asked.
		if engine.has_api_key and not self.api_key:
			self.api_key = secrets.token_hex(24)  # vLLM --api-key via VLLM_API_KEY env
		# Store what actually reaches --max-model-len; the suffix is input sugar. Blank stays
		# blank — that is how a placement asks for the engine default.
		if self.max_model_len:
			self.max_model_len = str(engine.max_model_len)
		if errors := engine.placement_errors:
			frappe.throw("<br>".join(errors))
		self.serve_command = engine.command
		validate_window(self)

	@property
	def gpu_vram_gb(self):
		"""From the Cloud Provider's cached type list. None until that cache is fetched, or for a
		type the provider no longer lists."""
		if not (self.cloud_provider and self.gpu_type_id):
			return None
		cached = frappe.db.get_value("Cloud Provider", self.cloud_provider, "gpu_types")
		for gpu_type in json.loads(cached or "[]"):
			if gpu_type.get("id") == self.gpu_type_id:
				return gpu_type.get("memoryInGb")
		return None

	@property
	def resolved_image(self):
		"""The Engine Image's ref, registry host included."""
		return frappe.get_cached_doc("Engine Image", self.engine_image).full_image

	# ── Standalone lifecycle ──
	@frappe.whitelist()
	def spawn(self):
		"""Spawn this pod on its provider (long job)."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.spawn_pod_doc",
			# The poll and the engine gate fill 1800 s on their own; the create retries add up to
			# provision_retries * SPAWN_RETRY_DELAY on top.
			queue="long", timeout=2100, pod_name=self.name,
		)
		frappe.msgprint(f"Spawning pod {self.name} on {self.cloud_provider}.", alert=True)

	@frappe.whitelist()
	def sync(self):
		"""Pull the pod's state off the provider and update this Pod. Runs inline for immediate
		feedback."""
		from grove.cloud_provider.provisioner import sync_pod

		res = sync_pod(self.name)
		frappe.msgprint(f"Synced pod {self.name}: {res['status']}.", alert=True)
		return res

	@frappe.whitelist()
	def stop(self):
		"""Free the GPU but keep the pod and its volume (weights survive); Start resumes it."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.stop_pod",
			queue="short", timeout=600, pod_name=self.name,
		)
		frappe.msgprint(f"Stopping pod {self.name} — volume kept, GPU released.", alert=True)

	@frappe.whitelist()
	def start(self):
		"""Resume a stopped pod and re-read its endpoints (the provider re-maps ports)."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.start_pod",
			queue="long", timeout=1800, pod_name=self.name,
		)
		frappe.msgprint(f"Starting pod {self.name} — ports will change, then sync.", alert=True)

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
		frappe.msgprint(f"Restarting pod {self.name} to apply config, then sync.", alert=True)

	@frappe.whitelist()
	def stream_logs(self):
		"""Follow the pod's container logs on the Logs tab: a background job relays the
		provider's stream over realtime for as long as the form keeps pinging keep_streaming.
		Deduplicated per pod, so a second Start (e.g. after a page reload) does not double up
		the stream."""
		from grove import log_relay

		if not self.pod_id:
			frappe.throw("Pod is not spawned yet — there are no logs to stream.")
		log_relay.keep_alive(self.doctype, self.name)
		frappe.enqueue(
			"grove.cloud_provider.provisioner.stream_pod_logs",
			queue="long", timeout=1800, job_id=f"pod-logs-{self.name}", deduplicate=True,
			pod_name=self.name,
		)

	@frappe.whitelist()
	def stop_logs(self):
		"""Tell the streaming job to finish — it checks between publishes."""
		from grove import log_relay

		log_relay.end(self.doctype, self.name)

	@frappe.whitelist()
	def terminate(self):
		"""Terminate the provider pod (frees GPU/disk/billing) and mark this Pod Terminated."""
		frappe.enqueue(
			"grove.cloud_provider.provisioner.terminate_pod_doc",
			queue="long", timeout=600, pod_name=self.name,
		)
		frappe.msgprint(f"Terminating pod {self.name}.", alert=True)


def validate_window(doc):
	"""Half a window is no window, and one of zero length would spawn and terminate on one tick."""
	if bool(doc.provision_at) != bool(doc.terminate_at):
		frappe.throw("Set both Up From and Down From, or neither.")
	if doc.provision_at and str(doc.provision_at) == str(doc.terminate_at):
		frappe.throw("Up From and Down From must differ.")
