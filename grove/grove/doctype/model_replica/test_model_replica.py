# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Engine env assembly and box-local port allocation. Pure — the deployment is passed in and
the sibling lookup is stubbed with a small table, so no site needed."""

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.serving.vllm import VllmEngine
from grove.grove.doctype.gpu.gpu import GPUUnavailable
from grove.grove.doctype.model_replica.model_replica import (
	CLAIM_HOLDING_STATUSES,
	ENGINE_PORT_BASE,
	GPU_CLAIMING_STATUSES,
	ModelReplica,
	_engine_env,
	_vllm_extravars,
	reconfigure_deployment,
	set_container_state,
)
from grove.serving.base import DEFAULT_MAX_MODEL_LEN, parse_context_length
from grove.serving.custom import CustomEngine

MODULE = "grove.grove.doctype.model_replica.model_replica"


def deployment(env=None, health_path=None, engine_image="img"):
	"""The Model Deployment a replica reads through. Only what `_engine_env` and
	`_vllm_extravars` actually reach for — these tests are about what the REPLICA contributes on
	top, and `engine_env_rows` is the layering that matters: deployment rows first, replica rows
	over them, so a replica adds to the deployment rather than replacing its list."""
	rows = [SimpleNamespace(key=key, value=value) for key, value in (env or {}).items()]
	return SimpleNamespace(
		engine_image=engine_image,
		health_path=health_path,
		env=rows,
		engine_env_rows=lambda replica: [*rows, *(replica.env or [])],
	)


def replica(env=None, attention_backend="auto", allow_long_max_model_len=0, deployment_env=None):
	return SimpleNamespace(
		attention_backend=attention_backend,
		allow_long_max_model_len=allow_long_max_model_len,
		env=[SimpleNamespace(key=key, value=value) for key, value in (env or {}).items()],
		deployment=deployment(env=deployment_env),
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
		env = _engine_env(replica(), engine(), "")
		self.assertEqual(env["VLLM_LOGGING_LEVEL"], "INFO")

	def test_derived_from_the_deployments_own_fields(self):
		md = replica(allow_long_max_model_len=1)
		env = _engine_env(md, engine(allow_long_max_model_len=1), "hf_xxx")
		self.assertEqual(env["HF_TOKEN"], "hf_xxx")
		self.assertEqual(env["VLLM_ALLOW_LONG_MAX_MODEL_LEN"], "1")

	def test_an_operator_row_can_turn_the_logging_level_up(self):
		# The baseline is a default, not a policy: the prompt text is a DEBUG-only line, so a
		# deployment being debugged can ask for it — and pay for it.
		md = replica({"VLLM_LOGGING_LEVEL": "DEBUG"})
		self.assertEqual(_engine_env(md, engine(), "")["VLLM_LOGGING_LEVEL"], "DEBUG")

	def test_attention_backend_is_a_serve_flag_not_env(self):
		# vLLM 0.24 dropped VLLM_ATTENTION_BACKEND — nothing in the package reads it, so
		# setting it here would leave the engine auto-selecting while the doc claimed
		# otherwise. VllmEngine passes --attention-backend instead.
		md = replica(attention_backend="FLASHINFER")
		self.assertNotIn("VLLM_ATTENTION_BACKEND", _engine_env(md, engine(), ""))

	def test_deployment_rows_reach_every_replica(self):
		# The point of moving env up: a row typed once on the deployment is in every replica's
		# --env-file without being retyped on any of them.
		env = _engine_env(replica(deployment_env={"HF_HUB_ENABLE_HF_TRANSFER": "1"}), engine(), "")
		self.assertEqual(env["HF_HUB_ENABLE_HF_TRANSFER"], "1")

	def test_a_replicas_rows_layer_on_top_of_the_deployments(self):
		# Additive, not replacing: a replica adds to the deployment's list rather than substituting
		# its own, and wins only on the keys it actually names.
		env = _engine_env(
			replica(
				deployment_env={"HF_HUB_ENABLE_HF_TRANSFER": "1", "VLLM_LOGGING_LEVEL": "INFO"},
				env={"VLLM_LOGGING_LEVEL": "DEBUG"},
			),
			engine(),
			"",
		)
		self.assertEqual(env["HF_HUB_ENABLE_HF_TRANSFER"], "1")
		self.assertEqual(env["VLLM_LOGGING_LEVEL"], "DEBUG")

	def test_operator_rows_win_over_the_derived_value(self):
		md = replica({"VLLM_ALLOW_LONG_MAX_MODEL_LEN": "0"}, allow_long_max_model_len=1)
		engine_ = engine(allow_long_max_model_len=1)
		self.assertEqual(_engine_env(md, engine_, "")["VLLM_ALLOW_LONG_MAX_MODEL_LEN"], "0")

	def test_operator_rows_are_carried_through(self):
		env = _engine_env(replica({"AWS_REGION": "us-east-1", "BLANK": None}), engine(), "")
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
			ModelReplica._assign_engine_port(doc)
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
	"""The claim IS `GPU.held_by`, taken by compare-and-swap, so two replicas cannot hold one card
	— only one caller can see the column empty. These pin WHICH statuses hold one; that the second
	swap loses is the database's job — see `gpu.claim`."""

	def acted(self, status):
		"""Which way sync_gpu_claims went for a replica in this status."""
		doc = SimpleNamespace(status=status, calls=[])
		doc.claim_gpus = lambda: doc.calls.append("claim")
		doc.release_gpus = lambda: doc.calls.append("release")
		ModelReplica.sync_gpu_claims(doc)
		return doc.calls[0]

	def test_a_draft_replica_already_holds_its_cards(self):
		# The reservation that closes the race. Before it, two placements reading the same free
		# list both insert; after it, the second cannot.
		self.assertEqual(self.acted("Draft"), "claim")

	def test_a_deploying_replica_holds_its_cards(self):
		# deploy_model flips to Provisioning mid-play; the cards were already claimed at insert
		# and must stay claimed across it.
		self.assertEqual(self.acted("Provisioning"), "claim")

	def test_a_serving_replica_holds_its_cards(self):
		self.assertEqual(self.acted("Active"), "claim")

	def test_a_broken_replica_still_holds_its_cards(self):
		# --restart unless-stopped: a crash-looping engine keeps coming back onto its cards, so
		# they are not free to hand out however dead the replica looks.
		self.assertEqual(self.acted("Broken"), "claim")

	def test_a_stopped_replica_releases_its_cards(self):
		# A stopped container holds no VRAM, so the card is genuinely free for a sibling. Start
		# is what re-takes it, and refuses if it cannot.
		self.assertEqual(self.acted("Inactive"), "release")

	def test_a_torn_down_replica_releases_its_cards(self):
		self.assertEqual(self.acted("Terminated"), "release")

	def test_every_status_the_doctype_offers_is_decided(self):
		# A status nobody classified would fall through to release and quietly free a card that
		# is still in use. Walk the Select rather than a list kept in step by hand.
		fields = json.loads((Path(__file__).parent / "model_replica.json").read_text())["fields"]
		[status] = [f for f in fields if f["fieldname"] == "status"]
		for option in status["options"].split("\n"):
			with self.subTest(option):
				self.assertIn(self.acted(option), ("claim", "release"))

	def test_holding_is_the_serving_set_plus_draft(self):
		self.assertEqual(CLAIM_HOLDING_STATUSES, ("Draft", *GPU_CLAIMING_STATUSES))


class TestGpuInventory(unittest.TestCase):
	"""A pinned card has to be on THIS replica's box.

	The display columns look after themselves now — `gpu_index`, `gpu_type` and `vram_gb` are
	`fetch_from` the linked GPU, so validation no longer copies them and they cannot drift. What
	validation still owes is the check a Link field will not make: the picker offers every card in
	the fleet, and only this box's are legal."""

	def validated(self, pinned, on_machine, max_model_len=None):
		doc = SimpleNamespace(
			inference_server="box",
			model_deployment="qwen3-35b-ap-south-1",
			gpus=[SimpleNamespace(gpu=name) for name in pinned],
			server=SimpleNamespace(machine="mc-1"),
			# One card pinned, and a deployment that asks for one — the shape check is exercised
			# on its own in TestAReplicaMustBeTheShapeItsDeploymentDeclares.
			deployment=SimpleNamespace(gpus_per_replica=1),
			serve_command="",
			max_model_len=max_model_len,
			engine=SimpleNamespace(
				placement_errors=[],
				tensor_parallel_size=1,
				command="serve …",
				max_model_len=parse_context_length(max_model_len) or DEFAULT_MAX_MODEL_LEN,
			),
		)
		doc._validate_shape = lambda: ModelReplica._validate_shape(doc)
		cards = [SimpleNamespace(name=n) for n in on_machine]
		with (
			patch.object(frappe, "throw", side_effect=frappe.ValidationError),
			patch(f"{MODULE}.cards_on", lambda machines: cards),
		):
			ModelReplica._validate_gpus(doc)
		return doc

	def test_a_context_length_suffix_is_stored_as_tokens(self):
		# Same rule the Pod side keeps: what the field holds after a save is what the engine ran
		# with, so nothing downstream parses it a second time.
		self.assertEqual(self.validated(["gpu-a"], ["gpu-a"], "128k").max_model_len, "131072")
		self.assertIsNone(self.validated(["gpu-a"], ["gpu-a"]).max_model_len)

	def test_a_card_on_this_box_is_accepted(self):
		self.assertEqual(self.validated(["gpu-b"], ["gpu-a", "gpu-b"]).gpus[0].gpu, "gpu-b")

	def test_a_card_on_another_box_is_refused(self):
		# The Link field offers every GPU in the fleet, so this is the only thing standing between
		# an operator and a replica pinned to hardware it cannot reach.
		with self.assertRaises(frappe.ValidationError):
			self.validated(["gpu-elsewhere"], ["gpu-a"])

	def test_the_same_card_twice_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.validated(["gpu-a", "gpu-a"], ["gpu-a"])


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
		claims = []
		md = SimpleNamespace(
			name="MD-00007",
			model="qwen3.5-4b",
			derived_engine_url="https://10.0.0.9/e/md-00007",
			get_password=lambda *args, **kwargs: "internal-key",
			# The play's status lands on the doc, then the claims are settled against it.
			sync_gpu_claims=lambda: claims.append(md.status),
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
			name="MD-00007", model="qwen3-35b", gpus=[], env=[], engine=engine,
			deployment=deployment(engine_image="img"),
			# No pinned cards, so CUDA_VISIBLE_DEVICES is empty and the box exposes whatever it has.
			gpu_records=[],
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


class TestStartRetakesTheCards(unittest.TestCase):
	"""Inactive releases its cards, so between Stop and Start a sibling can be placed on them.
	`docker start` would put a second engine on the card regardless — they split its VRAM and
	both OOM later, at a size each thought it had — so Start re-takes them before it queues the
	play, and refuses on the button when it cannot."""

	def start(self, claimed):
		"""What Start queued, given cards a sibling did or did not take while this was stopped."""
		queued = []

		def claim_gpus():
			if claimed:
				raise GPUUnavailable("GPU 0 on box was taken by md-a first.")

		doc = SimpleNamespace(
			inference_server="box",
			claim_gpus=claim_gpus,
			set_container_running=queued.append,
		)
		try:
			ModelReplica.start(doc)
		except GPUUnavailable:
			pass
		return queued

	def test_a_free_card_starts(self):
		self.assertEqual(self.start(claimed=False), [True])

	def test_a_taken_card_never_reaches_the_play(self):
		# Refused on the button, not in a worker: the operator is told why while they are
		# looking at the form, and no Ansible Play is queued for a start that cannot happen.
		self.assertEqual(self.start(claimed=True), [])


class TestStopAndStartRunAsAPlay(unittest.TestCase):
	"""Stop and Start change the box, so they go through a play like every other lifecycle
	button — `run_command` is for reads, and a run that leaves no Ansible Play leaves nobody
	able to see what docker actually did.

	The status write is the dangerous half: `status` is what _gateway_routes routes on, so it
	must follow the play, never lead it."""

	def run_state(self, running, rc):
		"""Every db.set_value a Stop/Start emits, plus the play it ran and its extra-vars."""
		md = SimpleNamespace(
			name="MD-00007",
			model="qwen3.5-4b",
			server=SimpleNamespace(name="INF-1", run_playbook=self.record_play(rc)),
		)
		written = []

		def set_value(doctype, name, values, value=None):
			written.append({values: value} if isinstance(values, str) else values)

		db = SimpleNamespace(set_value=set_value, commit=lambda: None)
		with (
			patch.object(frappe, "get_doc", lambda doctype, name=None: md),
			patch.object(frappe, "db", db),
			patch(f"{MODULE}.sync_published", create=True),
			patch("grove.grove.doctype.model.model.sync_published"),
		):
			set_container_state("MD-00007", running)
		return written

	def record_play(self, rc):
		def run_playbook(playbook, extravars=None, **kwargs):
			self.played, self.extravars = playbook, extravars
			return ("PLAY-1", rc)

		return run_playbook

	def test_it_runs_its_own_play(self):
		# Not teardown.yml with a flag: that one removes the run script, the env file and the
		# key, which is exactly what Stop promises to leave behind.
		self.run_state(running=False, rc=0)
		self.assertEqual(self.played, "container_state.yml")

	def test_the_play_is_told_which_instance_and_which_way(self):
		self.run_state(running=True, rc=0)
		self.assertEqual(self.extravars["vllm_instance"], "md-00007")
		self.assertIs(self.extravars["vllm_container_running"], True)

	def test_a_stop_lands_inactive_and_a_start_lands_active(self):
		self.assertEqual(self.run_state(running=False, rc=0), [{"status": "Inactive"}])
		self.assertEqual(self.run_state(running=True, rc=0), [{"status": "Active"}])

	def test_a_failed_run_writes_nothing(self):
		# The play proves the container reached the state. A stop that did not take must leave
		# the doc Active — writing Inactive would pull a serving engine out of the route table.
		self.assertEqual(self.run_state(running=False, rc=2), [])


class TestEveryStatusHasItsOwnColour(unittest.TestCase):
	"""The list view's indicator. Frappe falls back to guessing a colour from the status TEXT,
	and it knows none of these words — every unmapped status comes out the same grey, which is
	how Inactive and Broken became indistinguishable. Leaving a status unmapped is allowed and
	means "default grey"; what is checked is that the ones named are spelled like real statuses
	and coloured with something frappe will actually paint."""

	HERE = Path(__file__).parent

	def statuses(self):
		fields = json.loads((self.HERE / "model_replica.json").read_text())["fields"]
		[status] = [f for f in fields if f["fieldname"] == "status"]
		return status["options"].split("\n")

	# What frappe's indicator.scss actually renders. Anything else is a pill with no styling.
	RENDERABLE = frozenset(
		"green cyan blue orange yellow gray grey red pink darkgrey purple light-blue".split()
	)

	def colours(self):
		listview = (self.HERE / "model_replica_list.js").read_text()
		return dict(re.findall(r"\b(\w+): '([a-z-]+)',", listview))

	def test_every_status_it_names_is_one_the_doctype_offers(self):
		# Not every status needs naming — an unnamed one falls through to the default grey on
		# purpose. A MISNAMED one is the bug this catches: `Inactve: 'blue'` reads fine and
		# silently leaves Inactive grey again, which is the whole failure being fixed here.
		for status in self.colours():
			with self.subTest(status):
				self.assertIn(status, self.statuses())

	def test_every_colour_it_names_is_one_frappe_renders(self):
		# An unknown colour is not a fallback — the pill renders unstyled.
		for status, colour in self.colours().items():
			with self.subTest(status):
				self.assertIn(colour, self.RENDERABLE)

	def test_a_deliberate_stop_does_not_look_like_a_failure(self):
		# The pair this exists for. Same colour here is the bug, whatever the colours are.
		colours = self.colours()
		self.assertNotEqual(colours["Inactive"], colours["Broken"])


if __name__ == "__main__":
	unittest.main()


class TestAReplicaNamesItselfBeforeFetchFromRuns(unittest.TestCase):
	"""`autoname` runs on insert, ahead of every fetch_from on the doc. A replica created from a
	deployment is sent only the deployment, the box and the cards — so if the name were built from
	`self.model` as stored, it would be built from a blank."""

	def name(self, **sent):
		values = {"Model Deployment": "qwen3-35b", "Inference Server": "ap-south-1"}
		md = frappe._dict(sent)
		with (
			patch.object(frappe, "db", frappe._dict(
				get_value=lambda doctype, *a, **k: values[doctype])),
			patch(f"{MODULE}.next_replica_name", side_effect=lambda m, s, r: f"{m}|{r}|{s}"),
		):
			ModelReplica.autoname(md)
		return md

	def test_the_model_comes_off_the_deployment_not_the_unfetched_field(self):
		md = self.name(model_deployment="qwen3-35b-ap-south-1", inference_server="inf3")
		self.assertEqual(md.name, "qwen3-35b|ap-south-1|inf3")
		# And it is left ON the doc, so the mandatory check and sync_published both see it.
		self.assertEqual(md.model, "qwen3-35b")


class TestAReplicaMustBeTheShapeItsDeploymentDeclares(unittest.TestCase):
	"""Replicas of one deployment have to be interchangeable, or the aggregate an autoscaler
	divides by — replicas x capacity — stops being arithmetic. It is also what lets tensor
	parallel size live on the deployment alone rather than being stored on both."""

	def check(self, cards, declared):
		md = frappe._dict(
			model_deployment="qwen3-35b-ap-south-1",
			gpus=[SimpleNamespace(gpu_index=i) for i in range(cards)],
			deployment=frappe._dict(gpus_per_replica=declared),
		)
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
			ModelReplica._validate_shape(md)

	def test_the_declared_number_of_cards_is_fine(self):
		self.check(cards=4, declared=4)

	def test_too_few_or_too_many_is_refused(self):
		for cards in (2, 8):
			with self.assertRaises(frappe.ValidationError, msg=f"{cards} cards"):
				self.check(cards=cards, declared=4)

	def test_naming_no_cards_is_still_the_unpinned_single_gpu_case(self):
		# Back-compat: a deployment with no GPU rows runs --gpus all, and that has to survive a
		# deployment declaring a number above it.
		self.check(cards=0, declared=1)
		self.check(cards=0, declared=4)
