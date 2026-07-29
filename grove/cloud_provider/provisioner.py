# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Standalone cloud Pod lifecycle via provider APIs (e.g. RunPod). A Pod is self-contained:
it holds the spawn spec (GPUs / ports / image / template / startup cmd / volume / env) and,
for a serving pod, the vLLM config (translated to the container start command). The Pod
form's Spawn / Sync / Restart / Terminate buttons drive the functions here. Pods are NOT
backed by a Machine — Machine + Inference Server + Model Deployment are the on-prem path."""

import time

import requests

import frappe

from grove.cloud_provider.runpod import RunPodClient, RunPodError
from grove.grove.doctype.ssh_key.ssh_key import injected_public_keys

# How long spawn waits for vLLM to actually serve (weights download + load) after the pod is
# SSH-reachable, before giving up and leaving it Loading for a later Sync to pick up.
ENGINE_READY_TIMEOUT = 1500
ENGINE_POLL_INTERVAL = 15

# Fallback mount for the pod's persistent volume disk (HF_HOME lives under it so weights
# survive restart). A Pod can override via its volume_mount_path field.
VOLUME_MOUNT = "/data"


def _client(provider):
	api_key = provider.get_password("api_key", raise_exception=False)
	if not api_key:
		frappe.throw(f"Cloud Provider {provider.name} has no API key set.")
	return RunPodClient(api_key)


def _pod_client(pod):
	if not pod.cloud_provider:
		frappe.throw(f"Pod {pod.name} has no Cloud Provider.")
	provider = frappe.get_doc("Cloud Provider", pod.cloud_provider)
	if provider.provider_type != "runpod":
		frappe.throw(f"Unsupported provider: {provider.provider_type}")
	return _client(provider)


def _gateway_sync_serving(pod_name):
	"""On a serving Pod lifecycle transition: recompute the Model's `published` flag (a Running
	Pod is a live deployment) and push the route table so the endpoint reaches the gateway.
	Serving pods have no Model Deployment to drive either, so their lifecycle triggers both here.
	No-op for a non-serving pod (no model). Best-effort — logged, never fatal."""
	model = frappe.db.get_value("Pod", pod_name, "model")
	if not model:
		return
	from grove.grove.doctype.model.model import sync_published

	sync_published(model)  # Running pod → published=1; Stopped/Terminated → 0 (unless another live route)
	try:
		from grove import gateway_sync

		gateway_sync.full_sync(trigger="Provision")
	except Exception:
		frappe.log_error(title="gateway full_sync after pod change failed")


def _pod_env(pod):
	"""Env injected into the container: HF_HOME on the volume (weights survive restart) +
	VLLM_API_KEY / VLLM_ATTENTION_BACKEND for a serving pod + the HF token for gated models,
	then the Pod's own Env rows layered on top (user wins on conflict). PUBLIC_KEY is added
	by the caller (SSH keys)."""
	mount = pod.volume_mount_path or VOLUME_MOUNT
	env = {"HF_HOME": f"{mount}/hf"}
	if pod.model:
		key = pod.get_password("api_key", raise_exception=False)
		if key:
			env["VLLM_API_KEY"] = key
		if pod.attention_backend:
			env["VLLM_ATTENTION_BACKEND"] = pod.attention_backend
		if pod.allow_long_max_model_len:
			env["VLLM_ALLOW_LONG_MAX_MODEL_LEN"] = "1"
		if frappe.conf.get("hf_token") and frappe.db.get_value("Model", pod.model, "gated"):
			env["HUGGING_FACE_HUB_TOKEN"] = frappe.conf.get("hf_token")
	for row in pod.env or []:
		if row.key:
			env[row.key] = row.value or ""
	return env


def _registry_auth_id(pod, client):
	"""RunPod won't take inline pull credentials — they're registered under a name and
	referenced by id. Re-registered per spawn so rotated credentials always apply. None when
	the image is public (or set manually, which is public-pull only)."""
	if not pod.engine_image:
		return None
	image = frappe.get_cached_doc("Engine Image", pod.engine_image)
	credentials = image.registry_credentials
	if not credentials:
		return None
	return client.get_registry_auth_id(f"grove-{frappe.scrub(image.image_provider)}", *credentials)


def _spawn_kwargs(pod, pubkeys, name, client):
	"""Assemble the RunPod spawn call from a Pod doc. dockerStartCmd = the derived
	serve_command (Model set) else the manual startup_command."""
	if not pod.gpu_type_id:
		frappe.throw(
			f"Pod {pod.name} has no GPU Type ID — set it (provider GPU id, e.g. 'NVIDIA L40S')."
		)
	command = pod.serve_command if pod.model else (pod.startup_command or None)
	env = {"PUBLIC_KEY": pubkeys}
	env.update(_pod_env(pod))
	return dict(
		gpu_type_id=pod.gpu_type_id,
		gpu_count=pod.gpu_count or 1,  # homogeneous pod
		volume_in_gb=pod.volume_in_gb or 50,
		container_disk_in_gb=pod.container_disk_gb or 20,
		ports=[f"{int(p.internal_port)}/{p.protocol or 'tcp'}" for p in pod.ports],
		env=env,
		image_name=pod.resolved_image,
		template_id=pod.template_id or None,
		docker_start_cmd=command or None,
		container_registry_auth_id=_registry_auth_id(pod, client),
		volume_mount_path=pod.volume_mount_path or VOLUME_MOUNT,
		name=name,  # cloud_type defaults SECURE in spawn_pod (Community closed to new hosts)
	)


def _engine_ready(engine_url, timeout=5):
	"""True once vLLM is actually serving: GET /health → 200. This — not SSH-reachability —
	is real readiness; weights can take many minutes to download + load after the pod is up.
	/health needs no api-key."""
	try:
		return requests.get(f"{engine_url}/health", timeout=timeout).status_code == 200
	except requests.RequestException:
		return False


def _update_pod_doc(pod, pod_api, running):
	"""Write a pod's provider state onto the Pod doc: each Ports row's external port (from the
	provider's remap), the public IP + SSH port, and status. For a serving pod, 'up' means vLLM
	answers /health (not just SSH) — so status is Running only when it serves, else Loading;
	engine_url (the gateway route target) is set only when Running, so the gateway never routes
	to a still-loading engine (→ 503s)."""
	port_map = pod_api.get("port_map") or {}
	for row in pod.ports:
		row.external_port = port_map.get(str(int(row.internal_port))) or 0
	if pod_api.get("public_ip"):
		pod.public_ip = pod_api["public_ip"]
	if pod_api.get("ssh_port"):
		pod.ssh_port = pod_api["ssh_port"]
		pod.ssh_user = "root"
	if pod.model:
		ext = port_map.get(str(int(pod.serve_port or 8080)))
		url = f"http://{pod.public_ip}:{ext}" if (running and pod.public_ip and ext) else ""
		if url and _engine_ready(url):
			pod.status, pod.engine_url = "Running", url
		else:
			pod.status, pod.engine_url = ("Loading" if running else "Stopped"), ""
	else:
		pod.status = "Running" if running else "Stopped"
	pod.save(ignore_permissions=True)
	frappe.db.commit()


def _await_engine(pod_name, pod_api):
	"""Poll the engine /health until vLLM serves (status → Running) or ENGINE_READY_TIMEOUT
	passes (stays Loading; a later Sync flips it). Serving pods only. Ports don't move after
	spawn, so the spawn-time pod_api snapshot stays valid for the whole wait."""
	deadline = time.time() + ENGINE_READY_TIMEOUT
	while time.time() < deadline:
		if frappe.db.get_value("Pod", pod_name, "status") == "Running":
			return
		time.sleep(ENGINE_POLL_INTERVAL)
		_update_pod_doc(frappe.get_doc("Pod", pod_name), pod_api, running=True)


def spawn_pod_doc(pod_name):
	"""Spawn a Pod on its provider. Builds the request from the Pod (GPUs / ports / image /
	env + serve_command for a Model pod), records the pod id, polls ready, writes endpoints
	back onto the Pod, and registers a serving endpoint with the gateway."""
	pod = frappe.get_doc("Pod", pod_name)
	client = _pod_client(pod)
	pubkeys = injected_public_keys()
	if not pubkeys:
		frappe.throw("No active SSH Key found — add one so Ansible/ops can reach the pod.")
	spawn_kwargs = _spawn_kwargs(pod, pubkeys, name=pod.name, client=client)

	try:
		frappe.db.set_value("Pod", pod.name, "status", "Provisioning")
		frappe.db.commit()

		pod_api = client.spawn_pod(**spawn_kwargs)
		frappe.db.set_value("Pod", pod.name, "pod_id", pod_api["pod_id"])
		frappe.db.commit()

		ready = client.poll_pod_ready(pod_api["pod_id"])  # SSH-reachable
		_update_pod_doc(frappe.get_doc("Pod", pod.name), ready, running=True)
		if pod.model:
			# vLLM keeps loading weights after SSH is up — wait for /health so the status
			# flips Loading → Running and the gateway route registers only once it serves.
			_await_engine(pod.name, ready)
		_gateway_sync_serving(pod.name)
		return {"status": "success", "pod_id": pod_api["pod_id"], "public_ip": ready["public_ip"]}

	except RunPodError as e:
		frappe.db.set_value("Pod", pod.name, "status", "Stopped")
		frappe.db.commit()
		frappe.log_error(title=f"Pod spawn failed {pod_name}")
		return {"status": "error", "message": str(e)}


def sync_pod(pod_name, wait=False):
	"""Pull the pod's current provider state and update the Pod (external ports / IP / SSH /
	status / engine_url), then refresh the gateway (ports may have moved on restart)."""
	pod = frappe.get_doc("Pod", pod_name)
	if not pod.pod_id:
		frappe.throw(f"Pod {pod.name} has no provider pod id — spawn it first.")
	client = _pod_client(pod)
	try:
		pod_api = client.poll_pod_ready(pod.pod_id) if wait else client.get_pod(pod.pod_id)
	except RunPodError as e:
		if "404" not in str(e):
			raise
		# Not found on the provider → assume it was terminated outside Grove (e.g. RunPod console).
		frappe.db.set_value(
			"Pod", pod.name, {"pod_id": "", "status": "Terminated", "engine_url": ""}
		)
		frappe.db.commit()
		_gateway_sync_serving(pod.name)
		return {"status": "Terminated"}
	running = pod_api.get("desired_status") == "RUNNING" and bool(pod_api.get("public_ip"))
	_update_pod_doc(pod, pod_api, running)
	_gateway_sync_serving(pod.name)
	return {"status": frappe.db.get_value("Pod", pod.name, "status")}


def restart_pod(pod_name):
	"""Apply edited config. The provider bakes the start command at create, so this respawns:
	terminate the old pod, spawn a new one with the current serve_command, then re-read the
	(moved) endpoints. Weights re-download unless a network volume is attached (future)."""
	pod = frappe.get_doc("Pod", pod_name)
	if pod.pod_id:
		client = _pod_client(pod)
		try:
			client.terminate_pod(pod.pod_id)
		except RunPodError:
			pass  # already gone → still respawn
		frappe.db.set_value("Pod", pod.name, {"pod_id": "", "status": "Stopped"})
		frappe.db.commit()
	return spawn_pod_doc(pod_name)


def stop_pod(pod_name):
	"""Stop the provider pod: frees the GPU but keeps the pod and its volume (so weights
	survive), drops the gateway route, and marks it Stopped. start_pod() resumes it."""
	pod = frappe.get_doc("Pod", pod_name)
	if not pod.pod_id:
		frappe.throw(f"Pod {pod.name} has no provider pod id — nothing to stop.")
	_pod_client(pod).stop_pod(pod.pod_id)
	frappe.db.set_value("Pod", pod.name, {"status": "Stopped", "engine_url": ""})
	frappe.db.commit()
	_gateway_sync_serving(pod.name)
	return {"status": "Stopped"}


def start_pod(pod_name):
	"""Resume a stopped pod, then re-read its endpoints — the provider re-maps ports on
	start, so the old external ports are stale. For a serving pod, waits for vLLM to load."""
	pod = frappe.get_doc("Pod", pod_name)
	if not pod.pod_id:
		frappe.throw(f"Pod {pod.name} has no provider pod id — spawn it first.")
	client = _pod_client(pod)
	client.start_pod(pod.pod_id)
	frappe.db.set_value("Pod", pod.name, "status", "Provisioning")
	frappe.db.commit()
	ready = client.poll_pod_ready(pod.pod_id)
	_update_pod_doc(frappe.get_doc("Pod", pod.name), ready, running=True)
	if pod.model:
		_await_engine(pod.name, ready)
	_gateway_sync_serving(pod.name)
	return {"status": frappe.db.get_value("Pod", pod.name, "status")}


def terminate_pod_doc(pod_name):
	"""Terminate the provider pod (frees GPU/disk/billing), clear its id + engine_url, mark it
	Terminated, and drop its endpoint from the gateway."""
	pod = frappe.get_doc("Pod", pod_name)
	if not pod.pod_id:
		return {"status": "error", "message": "No provider pod to terminate"}
	client = _pod_client(pod)
	try:
		client.terminate_pod(pod.pod_id)
	except RunPodError as e:
		return {"status": "error", "message": str(e)}

	frappe.db.set_value("Pod", pod.name, {"pod_id": "", "status": "Terminated", "engine_url": ""})
	frappe.db.commit()
	_gateway_sync_serving(pod.name)
	return {"status": "success"}
