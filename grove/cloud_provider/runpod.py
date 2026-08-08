# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""RunPod GPU cloud provider API client (REST v2 — v1 is deprecated and being retired).
Spawns Secure-Cloud pods with a pool of direct-TCP ports (SSH + vLLM engine ports), injects
SSH public keys, and reads the public IP + external port mapping back off the API (RunPod
random-maps each exposed port). Pure HTTP client — no Frappe deps; the provisioner assembles
keys/env/ports.

The parsed shapes this returns (see _parse_pod, list_gpu_types) are Grove's own vocabulary and
outlive the provider's wire format — callers never see v2 field names."""

import json
import time

import requests

RUNPOD_API_URL = "https://api.runpod.io/v2"
# A quiet pod sends nothing for long stretches; cut the read here and let the caller resume.
LOG_READ_TIMEOUT = 65

# CUDA + sshd base image. RunPod pods already ship NVIDIA drivers; this just needs an
# OS + sshd so Ansible can connect. A Pod always names its own image (Engine Image link or
# the manual field), so this is the fallback for direct API callers only.
DEFAULT_IMAGE = "vllm/vllm-openai:latest"
DEFAULT_CONTAINER_DISK_GB = 2

# RunPod pod states that map onto a Pod status of their own. Everything else — CREATED,
# RESTARTING, a state RunPod adds later — is a pod on its way up.
POD_STATUS = {
	"RUNNING": "Running",
	"EXITED": "Stopped",
	"PAUSED": "Stopped",
	"DEAD": "Stopped",
	"TERMINATED": "Terminated",
}


def pod_status(runpod_state):
	"""RunPod pod state → Pod status. An unrecognised state reads Provisioning, never Stopped:
	a pod that is slow to come up (or a state this client has not seen) must not be recorded as
	one the operator stopped."""
	return POD_STATUS.get(runpod_state, "Provisioning")


class RunPodError(Exception):
	pass


class RunPodClient:
	def __init__(self, api_key):
		self.api_key = api_key

	def _request(self, method, path, json_body=None):
		"""One REST call. Bearer auth; raises RunPodError with the response body on
		failure (RunPod returns a JSON error message worth surfacing)."""
		headers = {
			"Authorization": f"Bearer {self.api_key}",
			"Content-Type": "application/json",
		}
		try:
			r = requests.request(
				method, f"{RUNPOD_API_URL}{path}", headers=headers, json=json_body, timeout=30
			)
		except requests.RequestException as e:
			raise RunPodError(f"RunPod API error: {e}")
		if not r.ok:
			raise RunPodError(f"RunPod {method} {path} → {r.status_code}: {r.text}")
		return r.json() if r.text else {}

	@staticmethod
	def proxy_url(pod_id, internal_port):
		"""RunPod's HTTPS endpoint for a port exposed as http. RunPod terminates TLS with its own
		certificate, so the pod serves plain http and carries none; the hostname is keyed on the
		pod id, so unlike a direct-tcp mapping it survives a restart.

		Cloudflare fronts it with a 100s connection cap: a response that starts streaming inside
		that window is fine, one that is still buffering at 100s returns 524."""
		return f"https://{pod_id}-{int(internal_port)}.proxy.runpod.net"

	def _container_config(
		self,
		ports=None,
		env=None,
		image_name=None,
		args=None,
		container_disk_in_gb=None,
		volume_in_gb=None,
		volume_mount_path=None,
		name=None,
		container_registry_auth_id=None,
	):
		"""The container settings RunPod accepts on both create and PATCH, in v2 spelling.
		Only the fields given are emitted, so a PATCH stays partial (omitted = left alone)."""
		body = {
			"name": name,
			"image": image_name,
			"args": args,
			"disk": container_disk_in_gb,
			"ports": ports,
			"env": env,
			"registry": container_registry_auth_id,
		}
		# v2 nests the persistent disk under mounts; its kind is fixed at create, so a restart
		# must keep sending the same one.
		persistent = {"size": volume_in_gb, "path": volume_mount_path}
		if persistent := {k: v for k, v in persistent.items() if v is not None}:
			body["mounts"] = {"persistent": persistent}
		return {k: v for k, v in body.items() if v is not None}

	def spawn_pod(
		self,
		name,
		gpu_type_id,
		gpu_count,
		volume_in_gb,
		ports,
		env=None,
		image_name=None,
		volume_mount_path="/data",
		container_disk_in_gb=DEFAULT_CONTAINER_DISK_GB,
		cloud_type="SECURE",
		template_id=None,
		args=None,
		container_registry_auth_id=None,
	):
		"""Create an on-demand pod. `ports` is the pool list, e.g. ['22/tcp', '8080/http'], built
		from the Pod's own Ports rows since the provider cannot hot-add later; `env` is a dict
		(e.g. {"PUBLIC_KEY": <keys>} for SSH). `args` is appended to the image's entrypoint —
		for a vLLM image whose entrypoint is `vllm serve`, that is the repo plus its flags.
		`template_id` supplies container defaults (v2 has no templateId field, so the template
		is fetched and spread under the explicit settings); `container_registry_auth_id` (see
		get_registry_auth_id) authenticates the pull for a private image. Returns the parsed
		pod (see _parse_pod) — the endpoints are absent until it runs, so poll_pod_ready()."""
		config = self._container_config(
			ports=ports, env=env, args=args, name=name,
			image_name=image_name or (None if template_id else DEFAULT_IMAGE),
			container_disk_in_gb=container_disk_in_gb,
			volume_in_gb=volume_in_gb, volume_mount_path=volume_mount_path,
			container_registry_auth_id=container_registry_auth_id,
		)
		body = {
			**self.template_config(template_id),
			**config,
			"gpu": {"id": gpu_type_id, "count": gpu_count},
			"cloud": cloud_type,
		}
		pod = self._request("POST", "/pods", body)
		if not pod.get("id"):
			raise RunPodError(f"Failed to spawn pod (no id): {pod}")
		return self._parse_pod(pod)

	def template_config(self, template_id):
		"""A template's container settings, to spread into a create body. v2 dropped the
		create-time templateId, so the template is read here and the caller's own settings win
		over it. Empty when no template is named."""
		if not template_id:
			return {}
		template = self._request("GET", f"/templates/{template_id}")
		return {
			key: template[key]
			for key in ("image", "args", "disk", "ports", "env", "registry")
			if template.get(key) is not None
		}

	def update_pod(self, pod_id, **config):
		"""Edit a live pod in place (same settings spawn_pod takes). RunPod resets the container
		to pick the change up, but keeps the pod id and the volume — so a volume-backed HF_HOME
		keeps its weights, unlike a terminate + create. PATCH is a partial update, so anything
		omitted is left alone. The GPU shape, cloud type and template are create-only and cannot
		be changed here. Returns the parsed pod — the reset clears the endpoints, so
		poll_pod_ready() before reading them."""
		body = self._container_config(**config)
		if not body:
			raise RunPodError(f"update_pod {pod_id} called with nothing to change")
		return self._parse_pod(self._request("PATCH", f"/pods/{pod_id}", body))

	def get_registry_auth_id(self, name, username, password):
		"""Register pull credentials with RunPod → the id passed as the pod's `registry` (RunPod
		takes no inline credentials). Names are unique per account, so an existing entry under
		`name` is dropped and rewritten — RunPod owns this state, and re-registering keeps it
		from drifting off the credentials Grove holds."""
		existing = self._request("GET", "/registries")
		if isinstance(existing, dict):
			existing = existing.get("registries") or []
		for auth in existing:
			if auth.get("name") == name:
				self._request("DELETE", f"/registries/{auth['id']}")
		auth = self._request(
			"POST", "/registries", {"name": name, "username": username, "password": password}
		)
		if not auth.get("id"):
			raise RunPodError(f"Failed to create registry credential (no id): {auth}")
		return auth["id"]

	def get_pod(self, pod_id):
		"""Fetch a pod → parsed (id, status, public_ip, ssh_port, port_map)."""
		return self._parse_pod(self._request("GET", f"/pods/{pod_id}"))

	@staticmethod
	def _parse_pod(pod):
		"""Extract reachable endpoints plus the GPU shape. v2 publishes the live port mapping
		under `runtime`, which is null until the pod runs; each entry maps an internal port to
		a random external one on a shared IP. The GPU fields are what the pod actually runs on —
		update_pod cannot change them, so callers compare against them to decide between an
		in-place edit and a respawn."""
		ports = ((pod.get("runtime") or {}).get("ports")) or []
		# Keys are str so JSON round-trips cleanly (engine_url lookup uses str).
		port_map = {
			str(p["private"]): int(p["public"])
			for p in ports
			if p.get("private") and p.get("public")
		}
		# The direct-TCP mappings share one real public IP; a RunPod http proxy port reports a
		# separate CGNAT address instead, and entry order is not stable between calls — so read
		# the IP off a tcp mapping rather than whichever entry happens to come first.
		direct = [p for p in ports if p.get("type") == "tcp"] or ports
		gpu = pod.get("gpu") or {}
		return {
			"pod_id": pod.get("id"),
			"status": pod.get("status"),
			"public_ip": next((p["ip"] for p in direct if p.get("ip")), None),
			"ssh_port": port_map.get("22"),
			"port_map": port_map,
			"gpu_count": gpu.get("count"),
			"gpu_type_id": gpu.get("id"),
		}

	def poll_pod_ready(self, pod_id, timeout_sec=300, poll_interval_sec=5):
		"""Poll until the pod has a public IP and a mapped SSH port (so Ansible can
		connect). Engine ports map at the same time; port_map carries them. Returns the
		parsed pod dict."""
		start = time.time()
		while time.time() - start < timeout_sec:
			try:
				pod = self.get_pod(pod_id)
				if pod.get("public_ip") and pod.get("ssh_port"):
					return pod
			except RunPodError:
				pass  # not ready yet
			time.sleep(poll_interval_sec)
		raise RunPodError(f"Pod {pod_id} did not become SSH-reachable within {timeout_sec}s")

	def stream_logs(self, pod_id, source=None, tail=100, last_event_id=None):
		"""Yield (event_id, {ts, source, line}) off the pod's SSE log stream as it arrives, and
		(event_id, None) on each separator or keep-alive — a tick that lets a caller blocked on a
		quiet stream act (stop, flush) without waiting for the next log line. Ends when the pod's
		stream closes or nothing at all arrives for LOG_READ_TIMEOUT — reconnect with the last
		event id and tail=0 to resume without re-reading the backfill."""
		headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "text/event-stream"}
		if last_event_id:
			headers["Last-Event-ID"] = last_event_id
		params = {"tail": tail}
		if source:
			params["source"] = source
		try:
			response = requests.get(
				f"{RUNPOD_API_URL}/pods/{pod_id}/logs",
				headers=headers, params=params, stream=True, timeout=(10, LOG_READ_TIMEOUT),
			)
		except requests.RequestException as e:
			raise RunPodError(f"RunPod API error: {e}")
		with response:
			if not response.ok:
				raise RunPodError(f"RunPod logs {pod_id} → {response.status_code}: {response.text}")
			event_id = None
			try:
				for line in response.iter_lines(decode_unicode=True):
					if line and line.startswith("id:"):
						event_id = line[3:].strip()
					elif line and line.startswith("data:"):
						yield event_id, json.loads(line[5:].strip() or "{}")
					else:
						yield event_id, None
			except requests.RequestException:
				return  # idle or dropped mid-stream — the caller resumes from the last event id

	def terminate_pod(self, pod_id):
		"""Terminate a pod — frees GPU, disk and billing."""
		self._request("DELETE", f"/pods/{pod_id}")
		return True

	def stop_pod(self, pod_id):
		"""Stop a pod: releases the GPU (status → EXITED) but keeps the pod and its volume, so
		it can be started again. Volume storage is still billed."""
		self._request("POST", f"/pods/{pod_id}/action", {"action": "stop"})
		return True

	def start_pod(self, pod_id):
		"""Start/resume a stopped pod. Needs its GPU type to be free again, and the port
		mapping is re-drawn — re-read endpoints after this."""
		self._request("POST", f"/pods/{pod_id}/action", {"action": "start"})
		return True

	def list_gpu_types(self):
		"""Available GPU types: {id, displayName, memoryInGb, secureCloud, communityCloud}.
		`id` is the exact string to put in a Pod's GPU Type ID. Grove's own key names — the
		cached list is read by the Cloud Provider form and Pod.gpu_vram_gb."""
		gpus = self._request("GET", "/catalog/gpus").get("gpus") or []
		return [
			{
				"id": gpu.get("id"),
				"displayName": gpu.get("name"),
				"memoryInGb": gpu.get("memory"),
				"secureCloud": gpu.get("secure"),
				"communityCloud": gpu.get("community"),
			}
			for gpu in gpus
		]
