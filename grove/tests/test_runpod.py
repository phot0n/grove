# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""RunPod request/response shaping, and what a Restart may apply in place. Pure — the HTTP
call is stubbed and both docs are passed in, so no site or network."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from grove.cloud_provider.provisioner import PodProvisioner
from grove.cloud_provider.runpod import RunPodClient, RunPodError


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


if __name__ == "__main__":
	unittest.main()
