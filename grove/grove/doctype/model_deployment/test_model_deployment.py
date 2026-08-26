# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Engine env assembly and box-local port allocation. Pure — the deployment is passed in and
the sibling lookup is stubbed with a small table, so no site needed."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.serving.vllm import VllmEngine
from grove.grove.doctype.model_deployment.model_deployment import (
	ENGINE_PORT_BASE,
	ModelDeployment,
	_engine_env,
	_vllm_extravars,
	reconfigure_deployment,
)
from grove.serving.base import DEFAULT_MAX_MODEL_LEN, parse_context_length
from grove.serving.custom import CustomEngine

MODULE = "grove.grove.doctype.model_deployment.model_deployment"


def deployment(env=None, attention_backend="auto", allow_long_max_model_len=0):
	return SimpleNamespace(
		attention_backend=attention_backend,
		allow_long_max_model_len=allow_long_max_model_len,
		env=[SimpleNamespace(key=key, value=value) for key, value in (env or {}).items()],
	)


def engine(**tuning):
	"""The real VllmEngine a deployment would build. These tests are about how the operator's Env
	rows layer over it, so the engine is the genuine article rather than a stub."""
	tuning.setdefault("port", 8080)
	return VllmEngine("qwen3-35b", {"hf_repo": "Qwen/Qwen3-35B"}, **tuning)


class TestEngineEnv(unittest.TestCase):
	def test_a_deployment_that_sets_nothing_still_logs_each_request(self):
		# The one thing every engine gets. INFO is enough for vLLM to log the request and its
		# generated output — the only per-request record on the box itself — without the
		# per-op debug lines that cost more than the record is worth.
		self.assertEqual(
			_engine_env(deployment(), engine(), ""),
			{
				"VLLM_LOGGING_LEVEL": "INFO",
				"HF_HUB_DISABLE_TELEMETRY": "1",
				"VLLM_NO_USAGE_STATS": "1",
			},
		)

	def test_derived_from_the_deployments_own_fields(self):
		md = deployment(allow_long_max_model_len=1)
		self.assertEqual(
			_engine_env(md, engine(allow_long_max_model_len=1), "hf_xxx"),
			{
				"VLLM_LOGGING_LEVEL": "INFO",
				"HF_HUB_DISABLE_TELEMETRY": "1",
				"VLLM_NO_USAGE_STATS": "1",
				"HF_TOKEN": "hf_xxx",
				"VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1",
			},
		)

	def test_an_operator_row_can_turn_the_logging_level_up(self):
		# The baseline is a default, not a policy: the prompt text is a DEBUG-only line, so a
		# deployment being debugged can ask for it — and pay for it.
		md = deployment({"VLLM_LOGGING_LEVEL": "DEBUG"})
		self.assertEqual(_engine_env(md, engine(), "")["VLLM_LOGGING_LEVEL"], "DEBUG")

	def test_attention_backend_is_a_serve_flag_not_env(self):
		# vLLM 0.24 dropped VLLM_ATTENTION_BACKEND — nothing in the package reads it, so
		# setting it here would leave the engine auto-selecting while the doc claimed
		# otherwise. VllmEngine passes --attention-backend instead.
		md = deployment(attention_backend="FLASHINFER")
		self.assertNotIn("VLLM_ATTENTION_BACKEND", _engine_env(md, engine(), ""))

	def test_operator_rows_win_over_the_derived_value(self):
		md = deployment({"VLLM_ALLOW_LONG_MAX_MODEL_LEN": "0"}, allow_long_max_model_len=1)
		engine_ = engine(allow_long_max_model_len=1)
		self.assertEqual(_engine_env(md, engine_, "")["VLLM_ALLOW_LONG_MAX_MODEL_LEN"], "0")

	def test_operator_rows_are_carried_through(self):
		env = _engine_env(deployment({"AWS_REGION": "us-east-1", "BLANK": None}), engine(), "")
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

	def fill(self, gpu_index, on_box, max_model_len=None):
		"""The deployment's GPU row after validation against the box's cards."""
		return self.validated(gpu_index, on_box, max_model_len).gpus[0]

	def validated(self, gpu_index, on_box, max_model_len=None):
		row = SimpleNamespace(gpu_index=gpu_index, gpu_model=None, vram_gb=None)
		doc = SimpleNamespace(
			inference_server="box",
			gpus=[row],
			server=SimpleNamespace(gpus=on_box),
			reject_claimed_gpus=lambda: None,
			tensor_parallel_size=0,
			serve_command="",
			max_model_len=max_model_len,
			engine=SimpleNamespace(
				placement_errors=[],
				tensor_parallel_size=1,
				command="serve …",
				max_model_len=parse_context_length(max_model_len) or DEFAULT_MAX_MODEL_LEN,
			),
		)
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
			ModelDeployment._validate_gpus(doc)
		return doc

	def test_a_context_length_suffix_is_stored_as_tokens(self):
		# Same rule the Pod side keeps: what the field holds after a save is what the engine ran
		# with, so nothing downstream parses it a second time.
		card = SimpleNamespace(gpu_index=0, gpu_model="H100", vram_gb=80)
		self.assertEqual(self.validated(0, [card], "128k").max_model_len, "131072")
		self.assertIsNone(self.validated(0, [card]).max_model_len)

	def test_display_columns_come_from_the_box(self):
		card = SimpleNamespace(gpu_index=1, gpu_model="H100", vram_gb=80)
		row = self.fill(1, [SimpleNamespace(gpu_index=0, gpu_model="H100", vram_gb=80), card])
		self.assertEqual((row.gpu_model, row.vram_gb), ("H100", 80))

	def test_a_gpu_the_box_does_not_have_is_refused(self):
		card = SimpleNamespace(gpu_index=0, gpu_model="H100", vram_gb=80)
		with self.assertRaises(frappe.ValidationError):
			self.fill(3, [card])


class TestReconfigureKeepsTheModelRoutable(unittest.TestCase):
	"""Update Engine Config must not take the model out of the gateway while it runs.

	`status` is read as "is this engine serving?" — `_gateway_routes` routes only Active — and
	the scheduler pushes the WHOLE route table every two minutes. So a status written before the
	play is a guaranteed multi-minute outage, for a run that replaces the container only when the
	rendered config actually moved. This cost a live 503 on qwen3.5-4b once; the test is here so
	it cannot come back quietly."""

	def record_play(self, rc):
		"""A run_playbook that remembers which play it was handed."""
		def run_playbook(playbook, **kwargs):
			self.played = playbook
			return ("PLAY-1", rc)

		return run_playbook

	def writes_during(self, rc):
		"""Every db.set_value payload a reconfigure emits, in order."""
		md = SimpleNamespace(
			name="MD-00007",
			model="qwen3.5-4b",
			derived_engine_url="https://10.0.0.9/e/md-00007",
			get_password=lambda *args, **kwargs: "internal-key",
			server=SimpleNamespace(
				name="INF-1",
				is_provisioned=1,
				run_playbook=self.record_play(rc),
			),
		)
		written = []

		def set_value(doctype, name, values, value=None):
			# Accepts both shapes frappe supports — a dict, or a field/value pair. The pair form
			# is how the removed Provisioning write was made, so this records it as a plain
			# failure of the assertions below instead of a TypeError.
			written.append({values: value} if isinstance(values, str) else values)

		db = SimpleNamespace(set_value=set_value, commit=lambda: None)
		with (
			patch.object(frappe, "get_doc", lambda doctype, name=None: md),
			patch.object(frappe, "db", db),
			patch(f"{MODULE}._vllm_extravars", return_value={}),
		):
			reconfigure_deployment("MD-00007")
		return written

	def test_nothing_is_written_before_the_play(self):
		# One write, and it is the post-play one. A second, earlier write is the bug.
		self.assertEqual(len(self.writes_during(rc=0)), 1)

	def test_the_deployment_never_leaves_active_on_a_good_run(self):
		self.assertNotIn("Provisioning", str(self.writes_during(rc=0)))

	def test_a_good_run_lands_active_with_the_migrated_url(self):
		[final] = self.writes_during(rc=0)
		self.assertEqual(final["status"], "Active")
		self.assertEqual(final["engine_url"], "https://10.0.0.9/e/md-00007")

	def test_it_runs_its_own_play_not_a_trimmed_serve(self):
		# A trimmed serve still pulled the image, checked the disk and ran the proxy roles —
		# minutes of work for a flag change, and too slow to use on a deploy that is stuck on its
		# health gate.
		self.writes_during(rc=0)
		self.assertEqual(self.played, "reconfigure.yml")

	def test_a_failed_run_is_broken_and_leaves_the_url_alone(self):
		# A play that failed may not have written the box's nginx location, so the URL must not
		# move — the gateway would forward to a route that is not there.
		self.assertEqual(self.writes_during(rc=2), [{"status": "Broken"}])


class TestExtraVarsFollowTheEngineKind(unittest.TestCase):
	"""What the role is told to do differs by engine kind, and the role's own switches are what
	say it — no new `when:` clauses on the play."""

	def extravars(self, engine, engine_kind="vllm"):
		md = SimpleNamespace(
			name="MD-00007", model="qwen3-35b", engine_image="img", health_path=None,
			gpus=[], env=[], engine=engine,
		)
		inf = SimpleNamespace(data_path="/opt/vllm", hf_home="/opt/vllm/hf")
		settings = SimpleNamespace(
			weights_s3_engine_environment={}, weights_bucket="grove-weights",
			scrape_auth_variables={},
		)
		image = SimpleNamespace(
			full_image="img:latest", size_gb=15.0, engine_kind=engine_kind,
			registry_credentials=None,
		)
		with (
			patch.object(frappe, "conf", {}),
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(frappe, "get_cached_doc", return_value=image),
		):
			return _vllm_extravars(md, SimpleNamespace(hf_repo="Qwen/Qwen3-35B"), inf, "k")

	def test_a_custom_image_is_never_asked_to_predownload(self):
		# The bug this exists for: the role derives the download repo from vllm_model, so leaving
		# this on for an image with no positional runs `hf download` with no argument and the
		# play dies. It also turns off the weights half of the disk pre-check, which is right —
		# the image half still runs, and a NIM is 15 GB.
		vars = self.extravars(CustomEngine("nemotron-asr", {}, port=8080), engine_kind="custom")
		self.assertEqual(vars["vllm_model"], "")
		self.assertFalse(vars["vllm_predownload_model"])
		self.assertEqual(vars["vllm_cache_bucket"], "")
		self.assertEqual(vars["vllm_engine_kind"], "custom")

	def test_a_custom_startup_command_becomes_the_argument_list(self):
		engine = CustomEngine("nemotron-asr", {}, port=8080, startup_command="--http-port 9000")
		self.assertEqual(self.extravars(engine)["vllm_serve_args"], ["--http-port", "9000"])

	def test_a_custom_image_gets_no_health_gate_unless_it_names_one(self):
		# A guessed path is worse than none: plenty of images 404 whatever we would try, and the
		# role treats blank as "do not gate".
		vars = self.extravars(CustomEngine("nemotron-asr", {}, port=8080), engine_kind="custom")
		self.assertEqual(vars["vllm_health_path"], "")

	def test_a_vllm_image_still_predownloads_and_gates_on_health(self):
		engine = VllmEngine("qwen3-35b", {"hf_repo": "Qwen/Qwen3-35B"}, port=8080)
		vars = self.extravars(engine)
		self.assertEqual(vars["vllm_model"], "Qwen/Qwen3-35B")
		self.assertTrue(vars["vllm_predownload_model"])
		self.assertEqual(vars["vllm_health_path"], "/health")
		self.assertEqual(vars["vllm_cache_bucket"], "grove-weights")


if __name__ == "__main__":
	unittest.main()
