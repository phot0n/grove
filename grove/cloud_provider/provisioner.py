# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Standalone cloud Pod lifecycle via provider APIs. A Pod is self-contained: it holds its own
spawn spec and, for a serving pod, the vLLM config. Pods are NOT backed by a Machine — Machine +
Inference Server + Model Replica are the on-prem path.

PodProvisioner owns one Pod's provider side. The form's buttons reach it through the module-level
functions at the bottom, which exist because frappe.enqueue resolves a dotted path to a module
function, not to a method."""

import time

import frappe
import requests

from grove import failure, log_relay
from grove.cloud_provider.runpod import RunPodClient, RunPodError, pod_status
from grove.grove.doctype.pod_activity.pod_activity import record
from grove.grove.doctype.ssh_key.ssh_key import injected_public_keys

# How long spawn waits for the engine to serve after the pod is SSH-reachable, before leaving it
# Loading for a later Sync.
ENGINE_READY_TIMEOUT = 1500
ENGINE_POLL_INTERVAL = 15
# Pause between asks when the provider refuses to create the pod (no capacity, mostly).
SPAWN_RETRY_DELAY = 30
# One forward pass on an engine already answering its health path: a blip budget, not a load one.
WARMUP_TIMEOUT = 120

# Fallback mount for the persistent volume; HF_HOME lives under it so weights survive a restart.
VOLUME_MOUNT = "/data"

# How long to wait before reconnecting a dropped log stream (see pod_log_events).
LOG_RECONNECT_DELAY = 2


class PodProvisioner:
	"""The provider side of one Pod. Construction touches nothing, so the decision helpers can be
	exercised without a site."""

	def __init__(self, pod, trigger="Manual"):
		self.pod = pod
		self.trigger = trigger  # who asked: the form's button, or the due-time tick
		self._client = None
		self._engine = None

	@property
	def engine(self):
		"""Built once per provisioner: a sync asks it four questions and each build reads the
		Engine Image and the Model."""
		if self._engine is None:
			self._engine = self.pod.engine
		return self._engine

	# ── Provider access ───────────────────────────────────────────────────────

	@property
	def client(self):
		"""The Pod's provider API client, built once per provisioner."""
		if self._client is None:
			self._client = self.build_client()
		return self._client

	def build_client(self):
		"""Only RunPod for now — an unsupported provider fails here rather than part-way through a
		lifecycle call."""
		if not self.pod.cloud_provider:
			frappe.throw(f"Pod {self.pod.name} has no Cloud Provider.")
		provider = frappe.get_doc("Cloud Provider", self.pod.cloud_provider)
		if provider.provider_type != "runpod":
			frappe.throw(f"Unsupported provider: {provider.provider_type}")
		api_key = provider.get_password("api_key", raise_exception=False)
		if not api_key:
			frappe.throw(f"Cloud Provider {provider.name} has no API key set.")
		return RunPodClient(api_key)

	@property
	def public_keys(self):
		"""Missing keys stop a spawn up front: a pod nobody can log into is not worth billing
		for."""
		keys = injected_public_keys()
		if not keys:
			frappe.throw("No active SSH Key found — add one so Ansible/ops can reach the pod.")
		return keys

	# ── Request assembly ──────────────────────────────────────────────────────

	@property
	def env(self):
		"""Whatever the engine needs, on the volume's paths so caches survive a restart, then the
		Pod's own Env rows on top. PUBLIC_KEY is added by config_kwargs.

		The paths are this placement's to choose and the names are the engine's. The attention
		backend is NOT here — it rides the serve command, because vLLM 0.24 dropped
		VLLM_ATTENTION_BACKEND."""
		pod = self.pod
		mount = pod.volume_mount_path or VOLUME_MOUNT
		env = self.engine.env(
			hf_home=f"{mount}/hf",
			cache_root=f"{mount}/vllm-cache",
			api_key=pod.get_password("api_key", raise_exception=False) or "",
			hf_token=frappe.conf.get("hf_token") or "",
			# The engine applies these only if it actually streams.
			streaming_env=frappe.get_single("Grove Settings").weights_s3_engine_environment,
		)
		for row in pod.env or []:
			if row.key:
				env[row.key] = row.value or ""
		return env

	@property
	def registry_auth_id(self):
		"""RunPod won't take inline pull credentials — they are registered under a name and
		referenced by id. Re-registered per call so rotated credentials always apply."""
		if not self.pod.engine_image:
			return None
		image = frappe.get_cached_doc("Engine Image", self.pod.engine_image)
		credentials = image.registry_credentials
		if not credentials:
			return None
		return self.client.get_registry_auth_id(
			f"grove-{frappe.scrub(image.image_provider)}", *credentials
		)

	@property
	def config_kwargs(self):
		"""What RunPod accepts on BOTH create and update, so a restart applies exactly what a spawn
		would. RunPod appends args to the image's entrypoint, so a custom image with neither runs
		as built."""
		pod = self.pod
		command = pod.serve_command or pod.startup_command or None
		return dict(
			volume_in_gb=pod.volume_in_gb or 50,
			container_disk_in_gb=pod.container_disk_gb or 20,
			ports=[f"{int(p.internal_port)}/{p.protocol or 'tcp'}" for p in pod.ports],
			env={"PUBLIC_KEY": self.public_keys, **self.env},
			image_name=pod.resolved_image,
			args=command or None,
			container_registry_auth_id=self.registry_auth_id,
			volume_mount_path=pod.volume_mount_path or VOLUME_MOUNT,
			name=pod.name,
		)

	@property
	def spawn_kwargs(self):
		"""config_kwargs plus the create-only field update does not accept: the GPU shape."""
		if not self.pod.gpu_type_id:
			frappe.throw(
				f"Pod {self.pod.name} has no GPU Type ID — set it (provider GPU id, e.g. "
				"'NVIDIA L40S')."
			)
		return dict(
			self.config_kwargs,
			gpu_type_id=self.pod.gpu_type_id,
			gpu_count=self.pod.gpu_count or 1,  # homogeneous pod
			# cloud_type defaults SECURE in spawn_pod (Community closed to new hosts)
		)

	# ── Reading provider state back ───────────────────────────────────────────

	@property
	def engine_endpoint(self):
		"""Where the gateway reaches this pod's engine, from the Ports row for the serve port.

		An http row goes through the provider's HTTPS proxy: TLS terminates there and the address
		is keyed on the pod id rather than a mapping that moves every restart. A tcp row keeps its
		direct public_ip:external_port, which is plaintext and does move."""
		pod = self.pod
		serve_port = int(pod.serve_port or 8080)
		row = next((p for p in pod.ports if int(p.internal_port) == serve_port), None)
		if not row:
			return ""
		if row.protocol == "http":
			return RunPodClient.proxy_url(pod.pod_id, serve_port) if pod.pod_id else ""
		if pod.public_ip and row.external_port:
			return f"http://{pod.public_ip}:{row.external_port}"
		return ""

	@property
	def health_path(self):
		"""Path polled to decide the container serves, "" when the provider's own state is the
		whole status. A custom image has to name its own — it takes just as long to come up, and
		without a gate the pod reads Running minutes before anything answers."""
		return self.pod.health_path or self.engine.health_path

	@property
	def is_warmup_due(self):
		"""Whether there is an engine here worth proving. One with no request cheap enough to shape
		says so by publishing none, and a bring-up that outran await_engine has nothing serving
		yet."""
		return bool(self.engine.warmup_request) and self.current_status == "Running"

	def get_warmup_error(self):
		"""Why this engine could not serve one real request, "" when it did. Posts what the Ansible
		path posts, at the endpoint the gateway is about to be handed.

		Carries the bearer /health never needed: an unauthenticated POST is a 401 on a perfectly
		good engine running with VLLM_API_KEY."""
		request = self.engine.warmup_request
		if not request:
			return ""
		key = self.pod.get_password("api_key", raise_exception=False)
		try:
			response = requests.post(
				self.engine_endpoint + request["path"],
				json=request["body"],
				headers={"Authorization": f"Bearer {key}"} if key else {},
				timeout=WARMUP_TIMEOUT,
			)
		except requests.RequestException as error:
			return str(error)
		if response.status_code == 200:
			return ""
		return f"{response.status_code} {response.text[:200]}"

	def apply_provider_state(self, pod_api, running):
		"""Write a pod's provider state onto the Pod doc: each Ports row's external port, the
		public IP + SSH port, and status. For a gated pod, 'up' means the ENGINE answers, not just
		SSH — so engine_url is set only when Running, and the gateway never routes to a
		still-loading engine.

		`running` is the caller's verdict on whether the container is up (a bring-up forces it,
		since its snapshot predates the provider flipping to RUNNING). When it is not, the
		provider's own state decides — a pod still coming up reads Provisioning, not Stopped.

		Reloads first: the status writes around it go through db.set_value, which leaves the
		loaded doc stale."""
		self.pod = frappe.get_doc("Pod", self.pod.name)
		pod = self.pod
		state = "Running" if running else pod_status(pod_api.get("status"))
		port_map = pod_api.get("port_map") or {}
		for row in pod.ports:
			row.external_port = port_map.get(str(int(row.internal_port))) or 0
		if pod_api.get("public_ip"):
			pod.public_ip = pod_api["public_ip"]
		if pod_api.get("ssh_port"):
			pod.ssh_port = pod_api["ssh_port"]
			pod.ssh_user = "root"
		if not self.health_path:
			pod.status = state
		else:
			pod.status, pod.engine_url = self.gated_status(state)
		pod.save(ignore_permissions=True)
		frappe.db.commit()

	def gated_status(self, state):
		"""Status + route target for a gated pod. The provider saying RUNNING only means the
		container started, so this reads Running only when the engine answers. Every other
		provider state passes through as-is — a stopped pod is Stopped, not Loading."""
		if state != "Running":
			return state, ""
		url = self.engine_endpoint
		if url and _is_engine_serving(url + self.health_path):
			return "Running", url
		return "Loading", ""

	def await_engine(self, pod_api):
		"""Poll until the engine serves or ENGINE_READY_TIMEOUT passes, leaving it Loading for a
		later Sync. Ports do not move while the pod stays up, so the caller's snapshot holds."""
		deadline = time.time() + ENGINE_READY_TIMEOUT
		while time.time() < deadline:
			if frappe.db.get_value("Pod", self.pod.name, "status") == "Running":
				return
			time.sleep(ENGINE_POLL_INTERVAL)
			self.apply_provider_state(pod_api, running=True)

	def await_ready(self):
		"""The tail every bring-up shares: wait for the provider to publish endpoints, write them
		onto the Pod, then wait for a gated engine to answer and prove one forward pass."""
		ready = self.client.poll_pod_ready(self.pod.pod_id)
		self.apply_provider_state(ready, running=True)
		if self.health_path:
			# The engine keeps loading weights after SSH is up, so the route registers only once
			# the gate passes.
			self.await_engine(ready)
		if self.is_warmup_due and (error := self.get_warmup_error()):
			# Loading with no engine_url is a Pod's Broken: the route table needs both, so the
			# projection tick has nothing to publish.
			#
			# Withheld for the bring-up, not for good — reconcile.sync_all re-decides on the
			# health path alone, so a pod that answers /health but cannot generate flips back to
			# Running. The Failure doc is what survives.
			self.set_state({"status": "Loading", "engine_url": ""})
			failure.report("Pod", self.pod.name, "Pod engine failed warmup", error)
		self.sync_model_published()
		return ready

	def sync_model_published(self):
		"""On a serving Pod lifecycle transition: recompute the Model's `published` flag — a
		Running Pod is a live deployment, and a serving pod has no Model Replica to drive it.
		No-op for a non-serving pod. The route itself follows from `engine_url`, which the
		projection tick reads off the Pod."""
		model = frappe.db.get_value("Pod", self.pod.name, "model")
		if not model:
			return
		from grove.grove.doctype.model.model import sync_published

		# Running pod → published=1; Stopped/Terminated → 0 (unless another live route).
		sync_published(model)

	def set_state(self, values):
		"""Status-ish writes go through db.set_value: cheap, and they skip validate so a
		lifecycle transition can't be blocked by an unrelated field."""
		frappe.db.set_value("Pod", self.pod.name, values)
		frappe.db.commit()

	# ── Lifecycle ─────────────────────────────────────────────────────────────

	def spawn(self):
		"""Create the pod on its provider from the Pod doc, record the provider id, and wait for
		it to serve."""
		spawn_kwargs = self.spawn_kwargs  # assembled (and validated) before anything is billed
		started = frappe.utils.now_datetime()
		self.set_state({"status": "Provisioning", "provision_attempts": 0})
		try:
			pod_api = self.create_provider_pod(spawn_kwargs)
			ready = self.await_ready()
		except RunPodError as e:
			return self.fail(e, "Spawn", started)
		self.log("Spawn", "Success", f"{self.current_status} at {ready['public_ip']}", started=started)
		return {
			"status": "success",
			"pod_id": pod_api["pod_id"],
			"public_ip": ready["public_ip"],
		}

	def create_provider_pod(self, spawn_kwargs):
		"""The one call that is retried: the provider refusing to create the pod is the failure a
		later ask can fix. A pod that got an id is brought up exactly once."""
		retries = int(self.pod.provision_retries or 0)
		for attempt in range(1, retries + 2):
			self.set_state({"provision_attempts": attempt})
			try:
				pod_api = self.client.spawn_pod(**spawn_kwargs)
			except RunPodError as error:
				self.log("Spawn", "Failure", error, attempt=attempt)
				if attempt > retries:
					raise
				time.sleep(SPAWN_RETRY_DELAY)
				continue
			self.set_state({"pod_id": pod_api["pod_id"]})
			return pod_api

	def log(self, event, outcome, detail="", attempt=None, started=None):
		"""One Pod Activity row for this call, carrying who triggered it."""
		record(self.pod.name, event, outcome, detail, attempt=attempt, started=started, trigger=self.trigger)

	def sync(self, wait=False):
		"""Pull the pod's current provider state onto the Pod (external ports / IP / SSH /
		status / engine_url) and recompute the Model's published flag. The route table follows
		on the next projection tick."""
		self.require_pod_id("spawn it first")
		try:
			pod_api = (
				self.client.poll_pod_ready(self.pod.pod_id) if wait
				else self.client.get_pod(self.pod.pod_id)
			)
		except RunPodError as e:
			if "404" not in str(e):
				raise
			self.mark_terminated("Gone on the provider — terminated outside Grove")
			return {"status": "Terminated"}
		running = pod_status(pod_api.get("status")) == "Running" and bool(pod_api.get("public_ip"))
		self.apply_provider_state(pod_api, running)
		self.sync_model_published()
		return {"status": self.current_status}

	def restart(self):
		"""Apply edited config to the live pod IN PLACE: RunPod's update takes the container
		start command, so the pod is PATCHed with the current serve_command, image, env and
		disks, and resets to pick them up. The pod id and the volume survive, so the weights
		under HF_HOME are not re-downloaded — the reason this is not a respawn.

		Never destructive: an edit the update cannot deliver is refused (see restart_blocker)
		rather than silently terminating a live pod. Re-checked here as well as at enqueue time,
		since the pod's state can move while the job waits."""
		if not self.pod.pod_id:
			return self.spawn()
		if reason := self.restart_blocker(self.client.get_pod(self.pod.pod_id)):
			frappe.throw(reason)

		config_kwargs = self.config_kwargs
		started = frappe.utils.now_datetime()
		try:
			# Clear the route target before the container goes down: the reset can take as long as
			# a weight load, and the tick republishes from engine_url once it serves again.
			self.set_state({"status": "Provisioning", "engine_url": ""})
			self.sync_model_published()

			self.client.update_pod(self.pod.pod_id, **config_kwargs)
			# RunPod re-draws the port map on a reset, so endpoints are re-read, not assumed.
			self.await_ready()
		except RunPodError as e:
			return self.fail(e, "Restart", started)
		self.log("Restart", "Success", started=started)
		return {"status": self.current_status, "pod_id": self.pod.pod_id}

	def stop(self):
		"""Stop the provider pod: frees the GPU but keeps the pod and its volume (so weights
		survive), clears its route target, and marks it Stopped. start() resumes it."""
		self.require_pod_id("nothing to stop")
		try:
			self.client.stop_pod(self.pod.pod_id)
		except RunPodError as e:
			return self.fail(e, "Stop")
		self.set_state({"status": "Stopped", "engine_url": ""})
		self.sync_model_published()
		self.log("Stop", "Success")
		return {"status": "Stopped"}

	def start(self):
		"""Resume a stopped pod, then re-read its endpoints — the provider re-maps ports on
		start, so the old external ports are stale. For a serving pod, waits for vLLM to load."""
		self.require_pod_id("spawn it first")
		started = frappe.utils.now_datetime()
		try:
			self.client.start_pod(self.pod.pod_id)
			self.set_state({"status": "Provisioning"})
			self.await_ready()
		except RunPodError as e:
			return self.fail(e, "Start", started)
		self.log("Start", "Success", started=started)
		return {"status": self.current_status}

	def terminate(self):
		"""Terminate the provider pod (frees GPU/disk/billing), clear its id + engine_url, and
		mark it Terminated. The tick prunes its route on the next pass."""
		if not self.pod.pod_id:
			return {"status": "error", "message": "No provider pod to terminate"}
		try:
			self.client.terminate_pod(self.pod.pod_id)
		except RunPodError as e:
			return self.fail(e, "Terminate")
		self.mark_terminated(f"Provider pod {self.pod.pod_id} terminated")
		return {"status": "success"}

	def mark_terminated(self, detail):
		"""The one write for a pod that is gone, whoever removed it: no id, no route, and the
		Model's published flag recomputed."""
		self.set_state({"pod_id": "", "status": "Terminated", "engine_url": ""})
		self.sync_model_published()
		self.log("Terminate", "Success", detail)

	# ── Guards ────────────────────────────────────────────────────────────────

	def restart_blocker(self, live):
		"""Why this Pod's config cannot be applied to its live provider pod, or None when it
		can. `live` is the parsed provider pod (see RunPodClient._parse_pod).

		Restart is deliberately non-destructive, so an edit RunPod's update cannot express is
		refused with the way out instead of being turned into a terminate + spawn. Moving GPUs
		is the case that matters: a respawn re-downloads the weights, which is too expensive to
		do off a button labelled Restart, so it has to be an explicit Terminate then Spawn."""
		move = (
			"RunPod cannot move a pod's GPUs, so this needs an explicit Terminate, then "
			"Spawn — which re-downloads the weights."
		)
		if live.get("status") != "RUNNING":
			return (
				f"Pod {self.pod.name} is not running, and an update only resets a live "
				"container. Start it first, then Restart to apply this config."
			)
		if live.get("gpu_count") and (self.pod.gpu_count or 1) != live["gpu_count"]:
			return f"GPU count changed ({live['gpu_count']} → {self.pod.gpu_count or 1}). {move}"
		if live.get("gpu_type_id") and self.pod.gpu_type_id != live["gpu_type_id"]:
			return f"GPU type changed ({live['gpu_type_id']} → {self.pod.gpu_type_id}). {move}"
		return None

	def validate_restart(self):
		"""Throw if Restart can't apply this Pod's config in place. Called before the job is
		enqueued, so the operator gets the reason in the form instead of a failed background
		job."""
		if not self.pod.pod_id:
			return
		live = self.client.get_pod(self.pod.pod_id)
		if reason := self.restart_blocker(live):
			frappe.throw(reason)

	def require_pod_id(self, remedy):
		"""Nothing to talk to the provider about without an id — say which action fixes it."""
		if not self.pod.pod_id:
			frappe.throw(f"Pod {self.pod.name} has no provider pod id — {remedy}.")

	@property
	def current_status(self):
		"""The Pod's status as stored, not as loaded — the lifecycle calls write through
		db.set_value and apply_provider_state."""
		return frappe.db.get_value("Pod", self.pod.name, "status")

	def fail(self, error, event, started=None):
		"""A provider call died mid-lifecycle: hand the message back to the caller instead of
		raising, since these run as background jobs. The terminal Pod Activity row for the call
		is written here, so every failed lifecycle call leaves one whatever its path.

		Only a lifecycle that never got a provider pod is parked Stopped. Once there is a pod id
		the failure says nothing about the container — a bring-up that outran its poll, or an API
		call that timed out, leaves a pod that is very likely still coming up. Calling that
		Stopped drops its gateway route, so the status is left for the scheduled reconcile to
		read off the provider."""
		title = f"Pod {event.lower()} failed {self.pod.name}"
		if not frappe.db.get_value("Pod", self.pod.name, "pod_id"):
			self.set_state({"status": "Stopped"})
		frappe.log_error(title=title)
		# Reported rather than decorated: this path deliberately does not raise, so there is no
		# exception for the decorator to catch. mark_broken is left off because a Pod has no Broken
		# status — the docstring above is the reason, and the reconcile owns the real one.
		failure.report("Pod", self.pod.name, title, str(error))
		self.log(event, "Failure", error, started=started)
		return {"status": "error", "message": str(error)}


def _is_engine_serving(health_url, timeout=5):
	"""True once the engine is actually serving: GET on its health path → 200. This — not
	SSH-reachability — is real readiness; weights can take many minutes to download + load after
	the pod is up. vLLM's /health needs no api-key."""
	try:
		return requests.get(health_url, timeout=timeout).status_code == 200
	except requests.RequestException:
		return False


def _provisioner(pod_name, trigger="Manual"):
	return PodProvisioner(frappe.get_doc("Pod", pod_name), trigger=trigger)


# Queue entry points. frappe.enqueue takes a dotted path to a module function, so the Pod
# form's buttons reach the provisioner through these.


def spawn_pod_doc(pod_name, trigger="Manual"):
	return _provisioner(pod_name, trigger).spawn()


def sync_pod(pod_name, wait=False):
	return _provisioner(pod_name).sync(wait=wait)


def restart_pod(pod_name):
	return _provisioner(pod_name).restart()


def validate_restart(pod_name):
	return _provisioner(pod_name).validate_restart()


def stop_pod(pod_name):
	return _provisioner(pod_name).stop()


def start_pod(pod_name):
	return _provisioner(pod_name).start()


def terminate_pod_doc(pod_name, trigger="Manual"):
	return _provisioner(pod_name, trigger).terminate()


def pod_log_events(client, pod_id, tail=100):
	"""The pod's log lines off the provider, reconnecting from the last event id each time the
	stream drops — it ends on its own whenever the pod goes quiet. Yields None on a keep-alive
	so the relay still ticks on a silent pod, and gives up after three instant empty
	reconnects: that means the pod has stopped producing logs (terminated) entirely."""
	last_id, dead_rounds = None, 0
	while dead_rounds < 3:
		opened_at, received = time.monotonic(), 0
		try:
			for event_id, event in client.stream_logs(pod_id, tail=tail, last_event_id=last_id):
				if not event:
					yield None
					continue
				last_id, received = event_id or last_id, received + 1
				yield event.get("line", "")
		except RunPodError as error:
			yield f"— {error} —"  # surfaced in the log view rather than dying in a worker
			return
		dead_rounds = dead_rounds + 1 if not received and time.monotonic() - opened_at < 5 else 0
		tail = 0  # backfill only on the first connect
		time.sleep(LOG_RECONNECT_DELAY)
		yield None  # tick, so a Stop pressed during a reconnect lands right away


def stream_pod_logs(pod_name, tail=100):
	"""Relay the provider's log stream to the Pod form until Stop (or the job times out)."""
	pod = frappe.get_doc("Pod", pod_name)
	client = PodProvisioner(pod).client
	log_relay.relay(pod_log_events(client, pod.pod_id, tail), "Pod", pod_name)
