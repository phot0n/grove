# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""RunPod GPU cloud provider API client (REST v1). Spawns Secure-Cloud pods with a pool
of direct-TCP ports (SSH + vLLM engine ports), injects SSH public keys, and reads the
public IP + external port mapping back off the API (RunPod random-maps each exposed
port → `portMappings`). Pure HTTP client — no Frappe deps; the provisioner assembles
keys/env/ports."""

import shlex
import time

import requests

RUNPOD_API_URL = "https://rest.runpod.io/v1"
# GPU-type listing has no REST v1 endpoint — use the legacy GraphQL API for that one read.
RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"

# CUDA + sshd base image. RunPod pods already ship NVIDIA drivers; this just needs an
# OS + sshd so Ansible can connect. A Pod always names its own image (Engine Image link or
# the manual field), so this is the fallback for direct API callers only.
# Ubuntu 24.04 — ships python3.12 (default, with python3-apt) + gcc-13 natively, which is
# what the vLLM Ansible role expects.
DEFAULT_IMAGE = "vllm/vllm-openai:latest"
DEFAULT_CONTAINER_DISK_GB = 2


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
	def build_ports(engine_pool_size, engine_base=8080):
		"""RunPod ports list: SSH + a pool of vLLM TCP ports. Declared at pod-create;
		RunPod can't hot-add ports to a running pod, so the whole pool is opened up front
		and deployments claim from it. e.g. ['22/tcp','8080/tcp','8081/tcp',...]."""
		return ["22/tcp"] + [f"{engine_base + i}/tcp" for i in range(engine_pool_size)]

	def spawn_pod(
		self,
		gpu_type_id,
		gpu_count,
		volume_in_gb,
		ports,
		env=None,
		image_name=None,
		volume_mount_path="/data",
		container_disk_in_gb=DEFAULT_CONTAINER_DISK_GB,
		cloud_type="SECURE",
		name=None,
		template_id=None,
		docker_start_cmd=None,
		container_registry_auth_id=None,
	):
		"""Create an on-demand pod. `ports` is the pool list (build_ports); `env` is a
		dict (e.g. {"PUBLIC_KEY": <keys>} for SSH). `template_id` supplies the image (then
		image_name is ignored); `docker_start_cmd` overrides the image's default start
		command; `container_registry_auth_id` (see get_registry_auth_id) authenticates the
		pull for a private image. Returns the parsed pod (see _parse_pod) — publicIp/ports
		may be absent until running, so poll_pod_ready()."""
		body = {
			"computeType": "GPU",
			"cloudType": cloud_type,
			"gpuTypeIds": [gpu_type_id],
			"gpuCount": gpu_count,
			"volumeInGb": volume_in_gb,
			"containerDiskInGb": container_disk_in_gb,
			"ports": ports,
			"volumeMountPath": volume_mount_path,
			"env": env or {},
		}
		# Template provides the image; otherwise use the given image (or the base default).
		if template_id:
			body["templateId"] = template_id
		else:
			body["imageName"] = image_name or DEFAULT_IMAGE
		if docker_start_cmd:
			body["dockerStartCmd"] = self._argv(docker_start_cmd)
		if container_registry_auth_id:
			body["containerRegistryAuthId"] = container_registry_auth_id
		if name:
			body["name"] = name
		pod = self._request("POST", "/pods", body)
		if not pod.get("id"):
			raise RunPodError(f"Failed to spawn pod (no id): {pod}")
		return self._parse_pod(pod)

	@staticmethod
	def _argv(docker_start_cmd):
		"""RunPod wants dockerStartCmd as an argv ARRAY, not a string. shlex keeps any quoted
		extra_serve_args intact. None passes through so a caller can omit the field."""
		if docker_start_cmd is None or isinstance(docker_start_cmd, list):
			return docker_start_cmd
		return shlex.split(docker_start_cmd)

	def update_pod(
		self,
		pod_id,
		ports=None,
		env=None,
		image_name=None,
		volume_mount_path=None,
		container_disk_in_gb=None,
		volume_in_gb=None,
		name=None,
		docker_start_cmd=None,
		container_registry_auth_id=None,
	):
		"""Edit a live pod in place. RunPod resets the container to pick the change up, but
		keeps the pod id and the volume — so a volume-backed HF_HOME keeps its weights, unlike
		a terminate + create. Only the fields passed are sent (PATCH is a partial update, so
		anything omitted is left alone). The GPU shape, cloud type and template are create-only
		and cannot be changed here. Returns the parsed pod — the reset clears the endpoints, so
		poll_pod_ready() before reading them."""
		body = {
			"ports": ports,
			"env": env,
			"imageName": image_name,
			"volumeMountPath": volume_mount_path,
			"containerDiskInGb": container_disk_in_gb,
			"volumeInGb": volume_in_gb,
			"name": name,
			"dockerStartCmd": self._argv(docker_start_cmd),
			"containerRegistryAuthId": container_registry_auth_id,
		}
		body = {k: v for k, v in body.items() if v is not None}
		if not body:
			raise RunPodError(f"update_pod {pod_id} called with nothing to change")
		return self._parse_pod(self._request("PATCH", f"/pods/{pod_id}", body))

	def get_registry_auth_id(self, name, username, password):
		"""Register pull credentials with RunPod → the id passed as containerRegistryAuthId at
		pod-create (RunPod takes no inline credentials). Names are unique per account, so an
		existing entry under `name` is dropped and rewritten — RunPod owns this state, and
		re-registering keeps it from drifting off the credentials Grove holds."""
		existing = self._request("GET", "/containerregistryauth")
		if isinstance(existing, dict):
			existing = existing.get("containerRegistryAuths") or []
		for auth in existing:
			if auth.get("name") == name:
				self._request("DELETE", f"/containerregistryauth/{auth['id']}")
		auth = self._request(
			"POST", "/containerregistryauth",
			{"name": name, "username": username, "password": password},
		)
		if not auth.get("id"):
			raise RunPodError(f"Failed to create registry auth (no id): {auth}")
		return auth["id"]

	def get_pod(self, pod_id):
		"""Fetch a pod → parsed (id, desired_status, public_ip, ssh_port, port_map)."""
		return self._parse_pod(self._request("GET", f"/pods/{pod_id}"))

	@staticmethod
	def _parse_pod(pod):
		"""Extract reachable endpoints plus the GPU shape. `portMappings` = {internal:
		external} (RunPod random-maps each exposed port); all ports share one `publicIp`.
		The GPU fields are what the pod actually runs on — update_pod cannot change them, so
		callers compare against them to decide between an in-place edit and a respawn."""
		port_map = pod.get("portMappings") or {}
		# Normalise keys to str so JSON round-trips cleanly (engine_url lookup uses str).
		port_map = {str(k): int(v) for k, v in port_map.items()}
		return {
			"pod_id": pod.get("id"),
			"desired_status": pod.get("desiredStatus"),
			"public_ip": pod.get("publicIp"),
			"ssh_port": port_map.get("22"),
			"port_map": port_map,
			"gpu_count": (pod.get("gpu") or {}).get("count"),
			"gpu_type_id": (pod.get("machine") or {}).get("gpuTypeId"),
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

	def terminate_pod(self, pod_id):
		"""Terminate a pod — frees GPU, disk and billing."""
		self._request("DELETE", f"/pods/{pod_id}")
		return True

	def stop_pod(self, pod_id):
		"""Stop a pod: releases the GPU (desiredStatus → EXITED) but keeps the pod and its
		volume, so it can be started again. Volume storage is still billed."""
		self._request("POST", f"/pods/{pod_id}/stop")
		return True

	def start_pod(self, pod_id):
		"""Start/resume a stopped pod. Needs its GPU type to be free again, and the port
		mapping is re-drawn — re-read endpoints after this."""
		self._request("POST", f"/pods/{pod_id}/start")
		return True

	def list_gpu_types(self):
		"""Available GPU types: {id, displayName, memoryInGb, secureCloud, communityCloud}.
		`id` is the exact string to put in a Machine GPU row's gpu_model. Uses GraphQL —
		the REST v1 API has no gpuTypes endpoint."""
		query = "{ gpuTypes { id displayName memoryInGb secureCloud communityCloud } }"
		try:
			r = requests.post(
				RUNPOD_GRAPHQL_URL,
				headers={"Content-Type": "application/json", "api_key": self.api_key},
				json={"query": query},
				timeout=30,
			)
			r.raise_for_status()
		except requests.RequestException as e:
			raise RunPodError(f"RunPod API error: {e}")
		data = r.json()
		if data.get("errors"):
			raise RunPodError(f"RunPod GraphQL error: {data['errors']}")
		return (data.get("data") or {}).get("gpuTypes") or []
