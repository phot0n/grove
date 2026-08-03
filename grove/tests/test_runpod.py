# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""RunPod request/response shaping, and what a Restart may apply in place. Pure — the HTTP
call is stubbed and both docs are passed in, so no site or network."""

import unittest
from types import SimpleNamespace

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


class TestArgv(unittest.TestCase):
	def test_a_string_becomes_argv(self):
		self.assertEqual(RunPodClient._argv("vllm serve Qwen/Qwen3-35B"), ["vllm", "serve", "Qwen/Qwen3-35B"])

	def test_quoted_args_survive_the_split(self):
		# extra_serve_args can carry a quoted value; splitting on spaces would break it.
		self.assertEqual(
			RunPodClient._argv('serve --chat-template "a b"'), ["serve", "--chat-template", "a b"]
		)

	def test_a_list_passes_through(self):
		self.assertEqual(RunPodClient._argv(["vllm", "serve"]), ["vllm", "serve"])

	def test_none_passes_through_so_the_field_can_be_omitted(self):
		self.assertIsNone(RunPodClient._argv(None))


class TestUpdatePod(unittest.TestCase):
	def test_start_command_is_patched_as_argv(self):
		client = FakeClient()
		client.update_pod("pod1", docker_start_cmd="vllm serve repo --port 8080")
		method, path, body = client.calls[0]
		self.assertEqual((method, path), ("PATCH", "/pods/pod1"))
		self.assertEqual(body, {"dockerStartCmd": ["vllm", "serve", "repo", "--port", "8080"]})

	def test_only_the_given_fields_are_sent(self):
		# PATCH is a partial update — an omitted field must be left alone, not blanked.
		client = FakeClient()
		client.update_pod("pod1", image_name="vllm/vllm-openai:v0.24.0", env={"HF_HOME": "/data/hf"})
		_method, _path, body = client.calls[0]
		self.assertEqual(set(body), {"imageName", "env"})

	def test_the_gpu_shape_is_never_sent(self):
		# RunPod cannot move GPUs on an update; the provisioner respawns for that instead.
		client = FakeClient()
		client.update_pod("pod1", ports=["22/tcp"], volume_in_gb=50, container_disk_in_gb=20)
		_method, _path, body = client.calls[0]
		self.assertNotIn("gpuCount", body)
		self.assertNotIn("gpuTypeIds", body)

	def test_an_empty_update_is_refused(self):
		client = FakeClient()
		with self.assertRaises(RunPodError):
			client.update_pod("pod1")
		self.assertEqual(client.calls, [])


class TestParsePod(unittest.TestCase):
	def test_gpu_shape_is_read_for_the_respawn_decision(self):
		parsed = RunPodClient._parse_pod(
			{"id": "pod1", "gpu": {"count": 2}, "machine": {"gpuTypeId": "NVIDIA L40S"}}
		)
		self.assertEqual((parsed["gpu_count"], parsed["gpu_type_id"]), (2, "NVIDIA L40S"))

	def test_a_pod_without_a_gpu_block_parses(self):
		parsed = RunPodClient._parse_pod({"id": "pod1"})
		self.assertIsNone(parsed["gpu_count"])
		self.assertIsNone(parsed["gpu_type_id"])

	def test_port_mappings_are_string_keyed(self):
		parsed = RunPodClient._parse_pod({"id": "pod1", "portMappings": {22: 40022, 8080: 41234}})
		self.assertEqual(parsed["port_map"], {"22": 40022, "8080": 41234})
		self.assertEqual(parsed["ssh_port"], 40022)


def restart_blocker(pod, live):
	"""PodProvisioner construction is inert (the client is built on first use), so the guard
	can be exercised on a bare stand-in Pod."""
	return PodProvisioner(pod).restart_blocker(live)


def pod(gpu_count=2, gpu_type_id="NVIDIA L40S"):
	return SimpleNamespace(name="POD-1", gpu_count=gpu_count, gpu_type_id=gpu_type_id)


def live(gpu_count=2, gpu_type_id="NVIDIA L40S", desired_status="RUNNING"):
	return {"desired_status": desired_status, "gpu_count": gpu_count, "gpu_type_id": gpu_type_id}


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
		reason = restart_blocker(pod(), live(desired_status="EXITED"))
		self.assertIn("not running", reason)
		self.assertIn("Start it first", reason)

	def test_a_blank_gpu_count_on_the_pod_still_means_one(self):
		self.assertIsNone(restart_blocker(pod(gpu_count=0), live(gpu_count=1)))
		self.assertIn("GPU count changed", restart_blocker(pod(gpu_count=0), live(gpu_count=2)))

	def test_gpu_fields_the_provider_omits_are_not_treated_as_a_change(self):
		self.assertIsNone(restart_blocker(pod(), live(gpu_count=None, gpu_type_id=None)))


if __name__ == "__main__":
	unittest.main()
