# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Engine env assembly and box-local port allocation. Pure — the deployment is passed in and
the sibling lookup is stubbed with a small table, so no site needed."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.model_deployment.model_deployment import (
	ENGINE_PORT_BASE,
	ModelDeployment,
	_engine_env,
)


def deployment(env=None, attention_backend="auto", allow_long_max_model_len=0):
	return SimpleNamespace(
		attention_backend=attention_backend,
		allow_long_max_model_len=allow_long_max_model_len,
		env=[SimpleNamespace(key=key, value=value) for key, value in (env or {}).items()],
	)


class TestEngineEnv(unittest.TestCase):
	def test_nothing_set_is_no_env(self):
		self.assertEqual(_engine_env(deployment(), ""), {})

	def test_derived_from_the_deployments_own_fields(self):
		md = deployment(allow_long_max_model_len=1)
		self.assertEqual(
			_engine_env(md, "hf_xxx"),
			{"HF_TOKEN": "hf_xxx", "VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1"},
		)

	def test_attention_backend_is_a_serve_flag_not_env(self):
		# vLLM 0.24 dropped VLLM_ATTENTION_BACKEND — nothing in the package reads it, so
		# setting it here would leave the engine auto-selecting while the doc claimed
		# otherwise. ServeCommand passes --attention-backend instead.
		md = deployment(attention_backend="FLASHINFER")
		self.assertNotIn("VLLM_ATTENTION_BACKEND", _engine_env(md, ""))

	def test_operator_rows_win_over_the_derived_value(self):
		md = deployment({"VLLM_ALLOW_LONG_MAX_MODEL_LEN": "0"}, allow_long_max_model_len=1)
		self.assertEqual(_engine_env(md, "")["VLLM_ALLOW_LONG_MAX_MODEL_LEN"], "0")

	def test_operator_rows_are_carried_through(self):
		env = _engine_env(deployment({"AWS_REGION": "us-east-1", "BLANK": None}), "")
		self.assertEqual(env["AWS_REGION"], "us-east-1")
		self.assertEqual(env["BLANK"], "")  # set-but-empty, not dropped


class TestEnginePortAllocation(unittest.TestCase):
	"""A box runs one engine per deployment, each on its own port. Teardown frees a port by
	clearing it and marking the deployment Terminated, so the allocator has to hand that port
	back out — and never one an engine is still bound to, Broken included."""

	def allocate(self, siblings):
		"""The port a new deployment on `box` would take, given its siblings there."""
		def fake_get_all(_doctype, filters=None, pluck=None):
			excluded = filters["status"][1]
			return [row["engine_port"] for row in siblings if row["status"] not in excluded]

		doc = SimpleNamespace(engine_port=0, inference_server="box", name="md-new")
		with patch.object(frappe, "get_all", fake_get_all):
			ModelDeployment._assign_engine_port(doc)
		return doc.engine_port

	def test_the_first_deployment_on_a_box_takes_the_base_port(self):
		self.assertEqual(self.allocate([]), ENGINE_PORT_BASE)

	def test_a_port_a_running_engine_holds_is_skipped(self):
		# Broken still has a container on the box holding its port — only teardown frees it.
		siblings = [
			{"status": "Active", "engine_port": ENGINE_PORT_BASE},
			{"status": "Broken", "engine_port": ENGINE_PORT_BASE + 1},
		]
		self.assertEqual(self.allocate(siblings), ENGINE_PORT_BASE + 2)

	def test_a_stopped_deployment_keeps_its_port(self):
		# Stop leaves the container on the box — Start has to find the same port free.
		siblings = [
			{"status": "Active", "engine_port": ENGINE_PORT_BASE},
			{"status": "Inactive", "engine_port": ENGINE_PORT_BASE + 1},
		]
		self.assertEqual(self.allocate(siblings), ENGINE_PORT_BASE + 2)

	def test_a_torn_down_deployments_port_is_reused(self):
		siblings = [
			{"status": "Active", "engine_port": ENGINE_PORT_BASE},
			{"status": "Terminated", "engine_port": 0},
			{"status": "Active", "engine_port": ENGINE_PORT_BASE + 2},
		]
		self.assertEqual(self.allocate(siblings), ENGINE_PORT_BASE + 1)


class TestGpuClaims(unittest.TestCase):
	"""Two engines on one card split its VRAM and both OOM at a size each thought it had. A
	stopped deployment frees no card in the long run — Start puts an engine back on it."""

	def claim(self, gpu_index, siblings):
		"""Whether a deployment on `box` may take `gpu_index`, given its siblings there."""
		def fake_get_all(doctype, filters=None, fields=None, pluck=None):
			if doctype == "Model Deployment":
				wanted = filters["status"][1]
				return [s["name"] for s in siblings if s["status"] in wanted]
			# frappe.get_all hands back attribute-access rows, not plain dicts.
			return [
				SimpleNamespace(parent=s["name"], gpu_index=s["gpu_index"])
				for s in siblings
				if s["name"] in filters["parent"][1]
			]

		doc = SimpleNamespace(
			inference_server="box", name="md-new", gpus=[SimpleNamespace(gpu_index=gpu_index)]
		)
		with patch.object(frappe, "get_all", fake_get_all):
			with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
				try:
					ModelDeployment.reject_claimed_gpus(doc)
				except frappe.ValidationError:
					return False
		return True

	def test_a_gpu_an_active_deployment_serves_on_is_refused(self):
		self.assertFalse(self.claim(0, [{"name": "md-a", "status": "Active", "gpu_index": 0}]))

	def test_a_stopped_deployment_still_owns_its_gpu(self):
		self.assertFalse(self.claim(0, [{"name": "md-a", "status": "Inactive", "gpu_index": 0}]))

	def test_a_torn_down_deployment_releases_its_gpu(self):
		self.assertTrue(self.claim(0, [{"name": "md-a", "status": "Terminated", "gpu_index": 0}]))

	def test_a_free_gpu_on_a_busy_box_is_allowed(self):
		self.assertTrue(self.claim(1, [{"name": "md-a", "status": "Active", "gpu_index": 0}]))


class TestGpuInventory(unittest.TestCase):
	"""Pinned GPUs are checked and their display columns filled from the Inference Server's
	inventory — a deployment never reads a Machine itself."""

	def fill(self, gpu_index, on_box):
		"""The deployment's GPU row after validation against the box's cards."""
		row = SimpleNamespace(gpu_index=gpu_index, gpu_model=None, vram_gb=None)
		doc = SimpleNamespace(
			inference_server="box",
			gpus=[row],
			server=SimpleNamespace(gpus=on_box),
			reject_claimed_gpus=lambda: None,
			tensor_parallel_size=0,
		)
		serve = SimpleNamespace(placement_errors=[], tensor_parallel_size=1)
		with patch(
			"grove.grove.doctype.model_deployment.model_deployment.ServeCommand.for_deployment",
			return_value=serve,
		):
			with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
				ModelDeployment._validate_gpus(doc)
		return row

	def test_display_columns_come_from_the_box(self):
		card = SimpleNamespace(gpu_index=1, gpu_model="H100", vram_gb=80)
		row = self.fill(1, [SimpleNamespace(gpu_index=0, gpu_model="H100", vram_gb=80), card])
		self.assertEqual((row.gpu_model, row.vram_gb), ("H100", 80))

	def test_a_gpu_the_box_does_not_have_is_refused(self):
		card = SimpleNamespace(gpu_index=0, gpu_model="H100", vram_gb=80)
		with self.assertRaises(frappe.ValidationError):
			self.fill(3, [card])


if __name__ == "__main__":
	unittest.main()
