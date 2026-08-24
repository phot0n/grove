# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""RunPod request/response shaping, and what a Restart may apply in place. Pure — the HTTP
call is stubbed and both docs are passed in, so no site or network."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from grove.cloud_provider.provisioner import PodProvisioner
from grove.grove.doctype.pod.pod import Pod
from grove.cloud_provider.runpod import RunPodClient, RunPodError, pod_status
from grove.serving.base import build_engine


class FakeClient(RunPodClient):
	"""Captures the request instead of sending it, and answers with a pod body."""

	def __init__(self, response=None):
		super().__init__("test-key")
		self.calls = []
		self.response = response if response is not None else {"id": "pod1"}

	def _request(self, method, path, json_body=None):
		self.calls.append((method, path, json_body))
		return self.response


def spawn_kwargs(**overrides):
	return dict(
		{
			"name": "POD-1",
			"gpu_type_id": "NVIDIA L40S",
			"gpu_count": 2,
			"volume_in_gb": 50,
			"ports": ["22/tcp", "8080/tcp"],
			"args": "Qwen/Qwen3-35B --port 8080",
		},
		**overrides,
	)


class TestSpawnPod(unittest.TestCase):
	def test_the_v2_body_shape(self):
		client = FakeClient()
		client.spawn_pod(**spawn_kwargs())
		method, path, body = client.calls[0]
		self.assertEqual((method, path), ("POST", "/pods"))
		self.assertEqual(body["gpu"], {"id": "NVIDIA L40S", "count": 2})
		self.assertEqual(body["mounts"], {"persistent": {"size": 50, "path": "/data"}})
		self.assertEqual(body["cloud"], "SECURE")
		# args is appended to the image entrypoint (`vllm serve`), and stays a string in v2.
		self.assertEqual(body["args"], "Qwen/Qwen3-35B --port 8080")

	def test_a_pod_with_no_id_is_an_error_not_a_silent_success(self):
		client = FakeClient(response={})
		with self.assertRaises(RunPodError):
			client.spawn_pod(**spawn_kwargs())


class TestUpdatePod(unittest.TestCase):
	def test_the_start_arguments_are_patched(self):
		client = FakeClient()
		client.update_pod("pod1", args="repo --port 8080")
		method, path, body = client.calls[0]
		self.assertEqual((method, path), ("PATCH", "/pods/pod1"))
		self.assertEqual(body, {"args": "repo --port 8080"})

	def test_only_the_given_fields_are_sent(self):
		# PATCH is a partial update — an omitted field must be left alone, not blanked.
		client = FakeClient()
		client.update_pod("pod1", image_name="vllm/vllm-openai:v0.24.0", env={"HF_HOME": "/data/hf"})
		_method, _path, body = client.calls[0]
		self.assertEqual(set(body), {"image", "env"})

	def test_the_gpu_shape_is_never_sent(self):
		# RunPod cannot move GPUs on an update; the provisioner respawns for that instead.
		client = FakeClient()
		client.update_pod("pod1", ports=["22/tcp"], volume_in_gb=50, container_disk_in_gb=20)
		_method, _path, body = client.calls[0]
		self.assertNotIn("gpu", body)
		self.assertNotIn("cloud", body)

	def test_an_empty_update_is_refused(self):
		client = FakeClient()
		with self.assertRaises(RunPodError):
			client.update_pod("pod1")
		self.assertEqual(client.calls, [])


class TestParsePod(unittest.TestCase):
	def test_gpu_shape_is_read_for_the_respawn_decision(self):
		parsed = RunPodClient._parse_pod(
			{"id": "pod1", "gpu": {"count": 2, "id": "NVIDIA L40S"}}
		)
		self.assertEqual((parsed["gpu_count"], parsed["gpu_type_id"]), (2, "NVIDIA L40S"))

	def test_a_pod_without_a_gpu_block_parses(self):
		parsed = RunPodClient._parse_pod({"id": "pod1"})
		self.assertIsNone(parsed["gpu_count"])
		self.assertIsNone(parsed["gpu_type_id"])

	def test_the_runtime_port_mapping_is_string_keyed(self):
		parsed = RunPodClient._parse_pod({
			"id": "pod1",
			"runtime": {"ports": [
				{"private": 22, "public": 40022, "ip": "1.2.3.4"},
				{"private": 8080, "public": 41234, "ip": "1.2.3.4"},
			]},
		})
		self.assertEqual(parsed["port_map"], {"22": 40022, "8080": 41234})
		self.assertEqual((parsed["ssh_port"], parsed["public_ip"]), (40022, "1.2.3.4"))

	def test_the_public_ip_comes_from_a_direct_tcp_mapping(self):
		# A RunPod http proxy port reports a CGNAT address, and entry order is not stable —
		# taking the first ip would hand the gateway an unreachable host.
		parsed = RunPodClient._parse_pod({
			"id": "pod1",
			"runtime": {"ports": [
				{"private": 19123, "public": 60413, "ip": "100.65.30.26", "type": "http"},
				{"private": 22, "public": 23507, "ip": "198.13.252.35", "type": "tcp"},
				{"private": 8080, "public": 23506, "ip": "198.13.252.35", "type": "tcp"},
			]},
		})
		self.assertEqual(parsed["public_ip"], "198.13.252.35")
		self.assertEqual(parsed["port_map"]["8080"], 23506)

	def test_a_pod_that_is_not_running_yet_has_no_endpoints(self):
		# v2 nulls `runtime` until the pod runs — that must read as "not ready", not crash.
		parsed = RunPodClient._parse_pod({"id": "pod1", "status": "PROVISIONING", "runtime": None})
		self.assertEqual(parsed["port_map"], {})
		self.assertIsNone(parsed["public_ip"])
		self.assertIsNone(parsed["ssh_port"])


class TestListGpuTypes(unittest.TestCase):
	def test_the_catalog_is_normalised_to_groves_own_keys(self):
		# The cached list is read by the Cloud Provider form and Pod.gpu_vram_gb.
		client = FakeClient(response={"gpus": [
			{"id": "NVIDIA L40S", "name": "L40S", "memory": 48, "secure": True, "community": False}
		]})
		self.assertEqual(client.list_gpu_types(), [{
			"id": "NVIDIA L40S", "displayName": "L40S", "memoryInGb": 48,
			"secureCloud": True, "communityCloud": False,
		}])
		self.assertEqual(client.calls[0][:2], ("GET", "/catalog/gpus"))


class FakeStream:
	"""Stands in for a streaming Response: context manager + iter_lines. A line that is an
	exception is raised from the iteration, the way a dropped connection would."""

	def __init__(self, lines, ok=True, status_code=200, text=""):
		self.lines, self.ok, self.status_code, self.text = lines, ok, status_code, text

	def __enter__(self):
		return self

	def __exit__(self, *exc_info):
		return False

	def iter_lines(self, decode_unicode=False):
		for line in self.lines:
			if isinstance(line, Exception):
				raise line
			yield line


class TestStreamLogs(unittest.TestCase):
	def stream(self, lines, **kwargs):
		"""Run stream_logs against a canned SSE body; returns (events, request kwargs)."""
		response = FakeStream(lines, **{k: v for k, v in kwargs.items() if k != "call"})
		with patch.object(requests, "get", return_value=response) as get:
			events = list(RunPodClient("test-key").stream_logs("pod1", **kwargs.get("call", {})))
		return events, get.call_args

	def test_id_and_data_lines_become_events(self):
		events, _call = self.stream([
			"id: 2026-06-01T12:02:03Z/1",
			'data: {"ts":"2026-06-01T12:02:03Z","source":"container","line":"Model loaded."}',
		])
		self.assertEqual(events, [("2026-06-01T12:02:03Z/1", {
			"ts": "2026-06-01T12:02:03Z", "source": "container", "line": "Model loaded."
		})])

	def test_separators_and_keepalive_comments_tick_without_a_line(self):
		# A quiet stream is mostly blank lines and ':' comments. They carry no log line, but
		# each one has to reach the caller — it is their only chance to notice Stop.
		events, _call = self.stream(["", ": keep-alive", 'data: {"line":"a"}'])
		self.assertEqual([line for _id, line in events], [None, None, {"line": "a"}])

	def test_a_dropped_connection_ends_the_stream_instead_of_raising(self):
		# The caller reconnects from the last event id, so mid-stream failure is not an error.
		events, _call = self.stream([
			'data: {"line":"a"}', requests.ConnectionError("read timed out"), 'data: {"line":"b"}',
		])
		self.assertEqual([line for _id, line in events], [{"line": "a"}])

	def test_a_rejected_request_raises(self):
		with self.assertRaises(RunPodError):
			self.stream([], ok=False, status_code=401, text="unauthorized")

	def test_a_resume_sends_the_cursor_and_skips_the_backfill(self):
		_events, call = self.stream([], call={"tail": 0, "last_event_id": "2026-06-01T12:02:03Z/1"})
		self.assertEqual(call.kwargs["headers"]["Last-Event-ID"], "2026-06-01T12:02:03Z/1")
		self.assertEqual(call.kwargs["params"], {"tail": 0})


def restart_blocker(pod, live):
	"""PodProvisioner construction is inert (the client is built on first use), so the guard
	can be exercised on a bare stand-in Pod."""
	return PodProvisioner(pod).restart_blocker(live)


def pod(gpu_count=2, gpu_type_id="NVIDIA L40S"):
	return SimpleNamespace(name="POD-1", gpu_count=gpu_count, gpu_type_id=gpu_type_id)


def live(gpu_count=2, gpu_type_id="NVIDIA L40S", status="RUNNING"):
	return {"status": status, "gpu_count": gpu_count, "gpu_type_id": gpu_type_id}


def serving_pod(
	protocol="http",
	pod_id="abc123",
	public_ip="1.2.3.4",
	external_port=41234,
	health_path=None,
	engine_kind="vllm",
	streaming=False,
):
	model = {"hf_repo": "Qwen/Qwen3-35B", "modality": "text"}
	if streaming:
		model["weights_s3_uri"] = "s3://grove-weights/models/Qwen--Qwen3-35B"
	return SimpleNamespace(
		name="POD-1",
		pod_id=pod_id,
		serve_port=8080,
		public_ip=public_ip,
		model="Qwen/Qwen3-35B",
		health_path=health_path,
		# The real class, not a stub: an Engine takes a plain mapping and no site, so the pod
		# simply carries the one its doctype property would have built.
		engine=build_engine(engine_kind, "qwen3-35b", model, port=8080),
		ports=[
			SimpleNamespace(internal_port=22, protocol="tcp", external_port=22001),
			SimpleNamespace(internal_port=8080, protocol=protocol, external_port=external_port),
		],
	)


class TestPodEnv(unittest.TestCase):
	"""Container env: weights and compile caches live on the persistent volume, so a restart
	skips both the re-download and torch.compile."""

	def env(self, engine_kind="vllm", rows=(), streaming=False):
		pod = serving_pod(engine_kind=engine_kind, streaming=streaming)
		pod.volume_mount_path = "/data"
		pod.allow_long_max_model_len = 0
		pod.get_password = lambda *args, **kwargs: None
		pod.env = [SimpleNamespace(key=k, value=v) for k, v in rows]
		from grove.cloud_provider import provisioner
		settings = SimpleNamespace(weights_s3_engine_environment={"AWS_ACCESS_KEY_ID": "AKIA"})
		with (
			patch.object(provisioner.frappe, "conf", {}),
			patch.object(provisioner.frappe, "get_single", return_value=settings),
		):
			return PodProvisioner(pod).env

	def test_a_vllm_pod_caches_on_the_volume(self):
		env = self.env()
		self.assertEqual(env["HF_HOME"], "/data/hf")
		self.assertEqual(env["HF_XET_HIGH_PERFORMANCE"], "1")
		self.assertEqual(env["VLLM_CACHE_ROOT"], "/data/vllm-cache")
		self.assertEqual(env["TRITON_CACHE_DIR"], "/data/vllm-cache/triton")
		self.assertEqual(env["TORCHINDUCTOR_CACHE_DIR"], "/data/vllm-cache/torchinductor")

	def test_a_custom_image_gets_none_of_the_vllm_vars(self):
		# hf_transfer may not be installed there; its env var would crash huggingface_hub.
		# Telemetry is the exception: a plain env lookup, safe on any huggingface_hub.
		env = self.env(engine_kind="custom")
		self.assertEqual(set(env), {"HF_HOME", "HF_HUB_DISABLE_TELEMETRY"})

	def test_a_pod_env_row_wins(self):
		env = self.env(rows=[("VLLM_CACHE_ROOT", "/elsewhere")])
		self.assertEqual(env["VLLM_CACHE_ROOT"], "/elsewhere")

	def test_a_streaming_pod_gets_the_bucket_env(self):
		# Decided by the Model carrying a mirror, not by matching "runai_streamer" in the stored
		# serve command — which an operator could also type into extra_serve_args.
		env = self.env(streaming=True)
		self.assertEqual(env["AWS_ACCESS_KEY_ID"], "AKIA")

	def test_a_plain_pod_gets_no_bucket_env(self):
		self.assertNotIn("AWS_ACCESS_KEY_ID", self.env())


class TestEngineEndpoint(unittest.TestCase):
	"""Which address the gateway is handed for a serving pod."""

	def test_an_http_port_is_reached_through_the_provider_tls_proxy(self):
		# The point of the whole arrangement: https, so the vLLM key is not on the wire in
		# clear, and no certificate on the pod.
		endpoint = PodProvisioner(serving_pod()).engine_endpoint
		self.assertEqual(endpoint, "https://abc123-8080.proxy.runpod.net")

	def test_the_proxy_address_ignores_the_mapping_that_moves_on_restart(self):
		# Keyed on the pod id alone, so a restart that re-maps every direct port leaves the
		# gateway route valid.
		moved = serving_pod(public_ip="9.9.9.9", external_port=50999)
		self.assertEqual(
			PodProvisioner(moved).engine_endpoint, "https://abc123-8080.proxy.runpod.net"
		)

	def test_a_tcp_port_keeps_its_direct_mapping(self):
		# The escape hatch for work that cannot start streaming inside Cloudflare's 100s cap.
		endpoint = PodProvisioner(serving_pod(protocol="tcp")).engine_endpoint
		self.assertEqual(endpoint, "http://1.2.3.4:41234")

	def test_no_address_before_the_provider_has_published_one(self):
		# A blank endpoint keeps the pod Loading, which is what stops a route reaching a cold
		# engine — so each half-known state has to stay blank rather than render something.
		self.assertEqual(PodProvisioner(serving_pod(pod_id="")).engine_endpoint, "")
		self.assertEqual(
			PodProvisioner(serving_pod(protocol="tcp", public_ip="")).engine_endpoint, ""
		)
		self.assertEqual(
			PodProvisioner(serving_pod(protocol="tcp", external_port=0)).engine_endpoint, ""
		)

	def test_a_serve_port_missing_from_the_ports_table_has_no_address(self):
		pod = serving_pod()
		pod.serve_port = 9999
		self.assertEqual(PodProvisioner(pod).engine_endpoint, "")


class TestHealthPath(unittest.TestCase):
	"""Which pods wait for their engine before reading Running."""

	def test_a_vllm_pod_gates_on_its_own_health(self):
		self.assertEqual(PodProvisioner(serving_pod()).health_path, "/health")

	def test_a_custom_image_gates_on_the_path_it_names(self):
		# The bug this exists for: an ASR container read Running the moment RunPod said the
		# container was up, minutes before anything answered on the port.
		pod = serving_pod(engine_kind="custom", health_path="/v1/health/ready")
		self.assertEqual(PodProvisioner(pod).health_path, "/v1/health/ready")

	def test_a_custom_image_that_names_none_is_not_gated(self):
		# vLLM's /health must not be assumed onto an image that has never heard of it — that
		# would leave the pod Loading forever.
		self.assertEqual(PodProvisioner(serving_pod(engine_kind="custom")).health_path, "")

	def test_a_vllm_pod_may_override_the_path(self):
		pod = serving_pod(health_path="/v1/models")
		self.assertEqual(PodProvisioner(pod).health_path, "/v1/models")


class TestGatedStatus(unittest.TestCase):
	"""What a gated pod's status is allowed to say, given the provider's own state."""

	def gated(self, state, serving, **pod_kwargs):
		from grove.cloud_provider import provisioner

		with patch.object(provisioner, "_is_engine_serving", return_value=serving):
			return PodProvisioner(serving_pod(**pod_kwargs)).gated_status(state)

	def test_the_gate_alone_may_write_running(self):
		self.assertEqual(self.gated("Running", serving=True), ("Running", "https://abc123-8080.proxy.runpod.net"))

	def test_a_container_the_provider_calls_running_is_loading_until_it_answers(self):
		# The bug this exists for: RunPod reports RUNNING as soon as the container starts, so a
		# pod still pulling the image read Running with a blank route target.
		self.assertEqual(self.gated("Running", serving=False), ("Loading", ""))

	def test_a_pod_with_no_published_endpoint_is_loading(self):
		self.assertEqual(self.gated("Running", serving=True, pod_id=""), ("Loading", ""))

	def test_every_other_provider_state_passes_through(self):
		for state in ("Provisioning", "Stopped", "Terminated"):
			self.assertEqual(self.gated(state, serving=True), (state, ""))


class TestPodStatus(unittest.TestCase):
	def test_running_and_the_stopped_states(self):
		self.assertEqual(pod_status("RUNNING"), "Running")
		for state in ("EXITED", "PAUSED", "DEAD"):
			self.assertEqual(pod_status(state), "Stopped")
		self.assertEqual(pod_status("TERMINATED"), "Terminated")

	def test_a_pod_still_coming_up_is_never_stopped(self):
		# The bug this exists for: a slow bring-up reported as Stopped, which drops its route.
		for state in ("CREATED", "RESTARTING", "SOME_NEW_STATE", None, ""):
			self.assertEqual(pod_status(state), "Provisioning")


class TestRestartBlocker(unittest.TestCase):
	def test_matching_config_is_applied_in_place(self):
		self.assertIsNone(restart_blocker(pod(), live()))

	def test_a_changed_gpu_count_is_refused(self):
		# A respawn would re-download the weights, so Restart must not silently do one.
		reason = restart_blocker(pod(gpu_count=4), live(gpu_count=2))
		self.assertIn("GPU count changed", reason)
		self.assertIn("Terminate", reason)

	def test_a_changed_gpu_type_is_refused(self):
		reason = restart_blocker(pod(gpu_type_id="NVIDIA H100 PCIe"), live(gpu_type_id="NVIDIA L40S"))
		self.assertIn("GPU type changed", reason)
		self.assertIn("Terminate", reason)

	def test_a_pod_that_is_not_running_is_refused(self):
		# An update resets a live container; it is not a way to bring a stopped pod back.
		reason = restart_blocker(pod(), live(status="EXITED"))
		self.assertIn("not running", reason)
		self.assertIn("Start it first", reason)

	def test_a_blank_gpu_count_on_the_pod_still_means_one(self):
		self.assertIsNone(restart_blocker(pod(gpu_count=0), live(gpu_count=1)))
		self.assertIn("GPU count changed", restart_blocker(pod(gpu_count=0), live(gpu_count=2)))

	def test_gpu_fields_the_provider_omits_are_not_treated_as_a_change(self):
		self.assertIsNone(restart_blocker(pod(), live(gpu_count=None, gpu_type_id=None)))


class TestServePortIsOpened(unittest.TestCase):
	"""A serve port the provider was never asked to open is refused at validate."""

	def test_a_custom_image_is_checked_too(self):
		# The bug this exists for: an ASR pod served on 8000 while its Serve Port stayed at the
		# 8080 default. engine_endpoint found no row for 8080, so the health gate had nothing to
		# poll and the pod sat Loading forever with a live container behind it.
		unopened = serving_pod(engine_kind="custom")
		unopened.serve_port = 8000
		with patch("grove.grove.doctype.pod.pod.frappe.throw") as throw:
			Pod.validate(unopened)
		self.assertIn("8000", throw.call_args.args[0])

	def test_a_serve_port_in_the_ports_table_passes(self):
		custom = serving_pod(engine_kind="custom")
		with patch("grove.grove.doctype.pod.pod.frappe.throw") as throw:
			Pod.validate(custom)
		throw.assert_not_called()
		self.assertEqual(custom.serve_command, "")


if __name__ == "__main__":
	unittest.main()


class TestPodWarmup(unittest.TestCase):
	"""One real request before the gateway route goes live. A 200 from /health proves a socket is
	open; this proves the engine can run a forward pass under the name the gateway routes on."""

	def provisioner(self, api_key="engine-key", **pod_kwargs):
		pod = serving_pod(**pod_kwargs)
		pod.get_password = lambda field, raise_exception=True: api_key
		return PodProvisioner(pod)

	def post(self, response=None, error=None, **kwargs):
		"""Run get_warmup_error against a stubbed POST; returns (reason, captured call)."""
		with patch("grove.cloud_provider.provisioner.requests.post") as posted:
			posted.side_effect = error
			posted.return_value = response
			return self.provisioner(**kwargs).get_warmup_error(), posted

	def test_it_posts_the_payload_at_the_endpoint_the_gateway_gets(self):
		# Same address engine_url carries, so a warmup that passes proves the route's own target.
		_, posted = self.post(SimpleNamespace(status_code=200, text="{}"))
		url, = posted.call_args.args
		self.assertEqual(url, "https://abc123-8080.proxy.runpod.net/v1/completions")
		self.assertEqual(posted.call_args.kwargs["json"]["max_tokens"], 1)

	def test_it_sends_the_bearer_health_never_needed(self):
		# /health is open, but the engine runs with VLLM_API_KEY set — an unauthenticated POST is
		# a 401 on a pod that serves perfectly well.
		_, posted = self.post(SimpleNamespace(status_code=200, text="{}"))
		self.assertEqual(posted.call_args.kwargs["headers"], {"Authorization": "Bearer engine-key"})

	def test_a_pod_without_a_key_sends_no_header(self):
		_, posted = self.post(SimpleNamespace(status_code=200, text="{}"), api_key=None)
		self.assertEqual(posted.call_args.kwargs["headers"], {})

	def test_a_serving_engine_gives_no_reason(self):
		reason, _ = self.post(SimpleNamespace(status_code=200, text="{}"))
		self.assertEqual(reason, "")

	def test_a_refusal_is_reported_with_its_status(self):
		reason, _ = self.post(SimpleNamespace(status_code=500, text="CUDA error: no kernel image"))
		self.assertIn("500", reason)
		self.assertIn("no kernel image", reason)

	def test_an_unreachable_engine_is_reported_not_raised(self):
		reason, _ = self.post(error=requests.ConnectionError("connection refused"))
		self.assertIn("connection refused", reason)

	def test_a_custom_engine_has_no_payload_to_prove(self):
		with patch("grove.cloud_provider.provisioner.PodProvisioner.current_status", "Running"):
			self.assertFalse(self.provisioner(engine_kind="custom").is_warmup_due)
			self.assertTrue(self.provisioner().is_warmup_due)

	def test_a_pod_that_never_served_is_not_warmed(self):
		# await_engine timed out — there is no engine to prove, and the failure would be noise.
		with patch("grove.cloud_provider.provisioner.PodProvisioner.current_status", "Loading"):
			self.assertFalse(self.provisioner().is_warmup_due)


class TestWarmupWithholdsTheRoute(unittest.TestCase):
	"""What a failed warmup does to a bring-up: no route, a Failure someone sees, and the pod left
	Loading rather than Running-but-broken."""

	def await_ready(self, reason):
		provisioner = PodProvisioner(serving_pod())
		provisioner._client = SimpleNamespace(poll_pod_ready=lambda pod_id: {"id": "pod1"})
		calls = {}
		with patch.multiple(
			PodProvisioner,
			apply_provider_state=lambda self, ready, running: None,
			await_engine=lambda self, ready: None,
			is_warmup_due=True,
			get_warmup_error=lambda self: reason,
			set_state=lambda self, values: calls.setdefault("state", values),
			sync_model_published=lambda self: calls.setdefault("synced", True),
		):
			with patch("grove.cloud_provider.provisioner.failure.report") as reported:
				provisioner.await_ready()
		return calls, reported

	def test_a_failed_warmup_takes_the_pod_out_of_the_route_table(self):
		# Loading with no engine_url is a Pod's Broken — the route table needs Running AND a url.
		calls, reported = self.await_ready("500 CUDA error")
		self.assertEqual(calls["state"], {"status": "Loading", "engine_url": ""})
		self.assertIn("500 CUDA error", reported.call_args.args)

	def test_the_published_flag_is_still_recomputed(self):
		# A pod that failed warmup must stop counting as a live deployment for its Model.
		calls, _ = self.await_ready("500 CUDA error")
		self.assertTrue(calls["synced"])

	def test_a_serving_engine_is_left_alone(self):
		calls, reported = self.await_ready("")
		self.assertNotIn("state", calls)
		self.assertTrue(calls["synced"])
		reported.assert_not_called()


class TestSyncOfAPodGoneFromTheProvider(unittest.TestCase):
	"""A pod the provider 404s is Terminated by sync(), and its Model's published flag is
	recomputed so it stops advertising a pod that no longer exists."""

	def test_a_vanished_pod_is_terminated_and_unpublished(self):
		provisioner = PodProvisioner(serving_pod())

		def gone(pod_id):
			raise RunPodError("404 pod not found")

		provisioner._client = SimpleNamespace(get_pod=gone)
		synced = []
		with patch.multiple(
			PodProvisioner,
			set_state=lambda self, values: None,
			sync_model_published=lambda self: synced.append(True),
		):
			self.assertEqual(provisioner.sync(), {"status": "Terminated"})
		self.assertEqual(synced, [True])
