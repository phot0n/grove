# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What a replica of a deployment actually runs. Pure — the deployment and the replica are passed in
as plain mappings and the two site reads (the image's kind, the Model's launch config) are stubbed,
so none of this needs a site.

The rule under test is one line of policy with a lot resting on it: blank or 0 on a replica means
inherit. Get it wrong in the lenient direction and a replica silently runs the engine's defaults
instead of the tuning its deployment was given; get it wrong in the strict direction and no replica
can ever differ from its deployment."""

import unittest
from unittest.mock import patch

import frappe

from grove.grove.doctype.model_deployment.model_deployment import (
	ADDITIVE,
	OVERRIDABLE,
	DEPLOYMENT_ONLY,
	ModelDeployment,
	gpu_indexes,
)
from grove.serving.base import DEFAULT_MAX_MODEL_LEN

MODULE = "grove.grove.doctype.model_deployment.model_deployment"

DEPLOYMENT = {
	"name": "qwen3-35b-ap-south-1",
	"model": "qwen3-35b",
	"engine_image": "vllm-0.24",
	"gpus_per_replica": 4,
	"min_vram_gb": 80.0,
	"pipeline_parallel_size": 1,
	"dtype": "float16",
	"kv_cache_dtype": "fp8",
	"gpu_memory_utilization": 0.92,
	"max_num_batched_tokens": 8192,
	"max_num_seqs": 256,
	"attention_backend": "FLASHINFER",
	"max_model_len": "131072",
	"allow_long_max_model_len": 0,
	"extra_serve_args": "--disable-log-requests",
	"startup_command": None,
	"env": [],
}


class FakeDeployment(frappe._dict):
	"""A deployment as a plain mapping, carrying the real methods. `_dict` gives attribute access
	and `.get`, which is all these two read off `self` — so the logic under test is the shipped
	one, with no site behind it."""

	resolved_config = ModelDeployment.resolved_config
	engine_for = ModelDeployment.engine_for
	_rejection = ModelDeployment._rejection
	_candidate = ModelDeployment._candidate


def deployment(**overrides):
	return FakeDeployment({**DEPLOYMENT, **overrides})


def replica(**fields):
	"""A replica as saved: every overridable column present but blank, which is what the form
	default and the migration both leave behind."""
	blank = dict.fromkeys(OVERRIDABLE, None)
	return frappe._dict({**blank, "engine_port": 8081, "gpus": [1, 2, 3, 4], "env": [], **fields})


def resolved(tmpl=None, rep=None):
	return (tmpl or deployment()).resolved_config(rep)


class TestBlankMeansInherit(unittest.TestCase):
	def test_a_replica_that_overrides_nothing_runs_the_deployments_tuning(self):
		config = resolved(rep=replica())
		for key in OVERRIDABLE:
			self.assertEqual(config[key], DEPLOYMENT[key], key)

	def test_a_replica_that_sets_a_knob_wins_for_that_knob_alone(self):
		config = resolved(rep=replica(gpu_memory_utilization=0.75))
		self.assertEqual(config["gpu_memory_utilization"], 0.75)
		# Everything else still inherits — an override is per field, not per replica.
		self.assertEqual(config["max_num_seqs"], 256)
		self.assertEqual(config["kv_cache_dtype"], "fp8")

	def test_zero_is_unset_not_a_value(self):
		# The whole reason a Float/Int column can carry "inherit" at all: none of these is ever
		# legitimately 0, so 0 is free to mean blank — the reading engine_port already has.
		config = resolved(rep=replica(gpu_memory_utilization=0, max_num_seqs=0))
		self.assertEqual(config["gpu_memory_utilization"], 0.92)
		self.assertEqual(config["max_num_seqs"], 256)

	def test_no_replica_at_all_is_the_deployments_own_preview(self):
		config = resolved()
		for key in OVERRIDABLE:
			self.assertEqual(config[key], DEPLOYMENT[key], key)

	def test_a_replica_can_never_override_the_shape_it_shares(self):
		# pipeline size, the long-context override, a custom image's startup command: a replica
		# differing on any of these would not be a replica of this deployment, so resolved_config
		# does not even look at the replica for them.
		loud = replica(**dict.fromkeys(DEPLOYMENT_ONLY, "SHOUT"))
		config = resolved(rep=loud)
		for key in DEPLOYMENT_ONLY:
			self.assertEqual(config[key], DEPLOYMENT[key], key)

	def test_the_three_sets_do_not_overlap_and_cover_the_engines_knobs(self):
		self.assertEqual(set(OVERRIDABLE) & set(DEPLOYMENT_ONLY), set())
		self.assertEqual(set(ADDITIVE) & (set(OVERRIDABLE) | set(DEPLOYMENT_ONLY)), set())
		self.assertEqual(set(resolved()), set(OVERRIDABLE) | set(DEPLOYMENT_ONLY) | set(ADDITIVE))


class TestExtraServeArgsAreAppendedNotReplaced(unittest.TestCase):
	"""The one field a replica ADDS to rather than overrides. Replacing would mean retyping the
	deployment's whole flag list — often a --speculative-config blob — to add a single flag."""

	def args(self, replica_args):
		return resolved(rep=replica(extra_serve_args=replica_args))["extra_serve_args"]

	def test_a_replica_that_adds_nothing_gets_the_deployments_flags(self):
		self.assertEqual(self.args(None), "--disable-log-requests")

	def test_a_replicas_flags_land_after_the_deployments(self):
		# Order is the whole mechanism: vLLM takes the LAST occurrence of a repeated flag, so
		# landing second is what lets a replica override one of the deployment's.
		self.assertEqual(
			self.args("--enforce-eager"), "--disable-log-requests --enforce-eager"
		)

	def test_a_deployment_with_no_flags_leaves_only_the_replicas(self):
		config = deployment(extra_serve_args=None).resolved_config(replica(extra_serve_args="--enforce-eager"))
		self.assertEqual(config["extra_serve_args"], "--enforce-eager")

	def test_neither_side_naming_flags_is_blank_not_a_stray_space(self):
		config = deployment(extra_serve_args=None).resolved_config(replica())
		self.assertEqual(config["extra_serve_args"], "")


class TestEngineForAReplica(unittest.TestCase):
	"""The deployment builds the engine for both itself and its replicas, so a preview and what
	runs cannot be computed two different ways."""

	def engine(self, tmpl=None, rep=None):
		with (
			patch(f"{MODULE}.engine_tuning", return_value=("vllm", {})),
			patch(f"{MODULE}.launch_config", return_value={"hf_repo": "Qwen/Qwen3-35B"}),
		):
			return (tmpl or deployment()).engine_for(rep)

	def test_the_deployments_own_preview_uses_its_declared_shape(self):
		engine = self.engine()
		self.assertEqual(engine.gpu_count, 4)
		self.assertEqual(engine.tensor_parallel_size, 4)
		self.assertEqual(engine.gpu_vram_gb, 80.0)

	def test_a_replicas_engine_uses_the_replicas_cards_and_port(self):
		engine = self.engine(rep=replica(engine_port=8085, gpus=[0, 1]))
		self.assertEqual(engine.port, 8085)
		self.assertEqual(engine.gpu_count, 2)
		self.assertEqual(engine.tensor_parallel_size, 2)

	def test_an_override_reaches_the_serve_command(self):
		# The end of the whole chain: a knob set on the replica has to come out in the argv the
		# box runs, not just in resolved_config.
		self.assertIn("--gpu-memory-utilization 0.75", self.engine(rep=replica(gpu_memory_utilization=0.75)).command)
		self.assertIn("--gpu-memory-utilization 0.92", self.engine(rep=replica()).command)

	def test_a_replica_naming_no_cards_is_still_single_gpu(self):
		# Back-compat: a deployment with no GPU rows means unpinned single-GPU, and that has to
		# survive the deployment arriving above it.
		self.assertEqual(self.engine(rep=replica(gpus=[])).gpu_count, 1)

	def test_a_blank_max_model_len_on_both_is_the_engine_default(self):
		# Blank on the replica inherits, and blank on the DEPLOYMENT asks for the engine default —
		# the two compose rather than colliding.
		engine = self.engine(tmpl=deployment(max_model_len=None), rep=replica())
		self.assertEqual(engine.max_model_len, DEFAULT_MAX_MODEL_LEN)


class TestGpuIndexes(unittest.TestCase):
	"""A whitelisted method is handed strings from the client and a list from Python."""

	def test_the_shapes_a_caller_can_send(self):
		for sent in ([0, 1, 2], "0,1,2", "[0, 1, 2]", " 0 , 1 , 2 "):
			self.assertEqual(gpu_indexes(sent), [0, 1, 2], sent)

	def test_nothing_means_unpinned(self):
		for sent in (None, "", [], "  "):
			self.assertEqual(gpu_indexes(sent), [], repr(sent))


class TestNaming(unittest.TestCase):
	"""`MD-{#####}`, counted, carrying neither the model nor the shape. `gpus_per_replica` is
	editable and a name is not, so a name saying `4xh100` would go stale the first time someone
	re-shaped the deployment; the region is out for the same reason a deployment is not scoped
	to one."""

	def name(self, model, number="00014"):
		with patch(f"{MODULE}.next_deployment_name", side_effect=lambda: f"MD-{number}"):
			doc = FakeDeployment(model=model)
			ModelDeployment.autoname(doc)
		return doc.name

	def test_a_deployment_is_named_off_the_series(self):
		self.assertEqual(self.name("frappe/qwen3-35b"), "MD-00014")

	def test_the_name_says_nothing_about_the_model(self):
		# Two models, one number: the name is an id, not a label. Which model it serves is a
		# field, and the list view carries it.
		self.assertEqual(self.name("frappe/qwen3-35b"), self.name("openai/gpt-oss-120b"))

	def test_several_deployments_of_one_model_get_distinct_names(self):
		# The point of the counter: a second shape of a model, or a rollout running old and new
		# side by side, are both normal and neither may collide.
		self.assertNotEqual(
			self.name("frappe/qwen3-35b", "00014"), self.name("frappe/qwen3-35b", "00015")
		)


def claim(free=(), unpinned=0, replicas=0, vram=80, capability=9.0):
	"""What one box reports: its unheld cards, and what is already running on it."""
	cards = [
		{"name": f"gpu-{i}", "gpu_index": i, "gpu_type": "h100", "vram_gb": vram} for i in free
	]
	return frappe._dict(
		free_gpus=cards,
		vram_by_card={f"gpu-{i}": vram for i in free},
		capability_by_card={f"gpu-{i}": capability for i in free},
		replicas=replicas,
		unpinned=unpinned,
	)


def _real_engine(torch_dtype):
	"""`engine_for` returning a REAL VllmEngine, so a rejection proves the whole path — the
	deployment's dtype, the Model's, and the capability the scheduler measured off the cards."""
	from grove.serving.vllm import VllmEngine

	def engine_for(self, replica=None, gpu_vram_gb=None, compute_capability=None):
		return VllmEngine(
			"qwen3-35b",
			{"hf_repo": "Qwen/Qwen3-35B", "modality": "text", "torch_dtype": torch_dtype},
			port=8080,
			gpu_count=self.gpus_per_replica,
			gpu_vram_gb=gpu_vram_gb,
			compute_capability=compute_capability,
			dtype=self.get("dtype"),
		)

	return engine_for


class TestWhyABoxIsRejected(unittest.TestCase):
	"""The rejection strings ARE the error message when nothing fits, so they are asserted rather
	than just their truthiness — a scheduler that only says "no capacity" is the infuriating kind."""

	def reject(self, dep=None, errors=(), **box):
		"""The engine's own arithmetic has its own tests; stubbed clean here so each hard filter
		is asserted on its own. Patched on the CLASS — `engine_for` is a class attribute, so an
		instance assignment would set a dict key attribute lookup never reaches."""
		dep = dep or deployment()
		reported = box.get("claim", claim(free=(0, 1, 2, 3)))
		# The engine's own arithmetic is stubbed so each hard filter is asserted on its own —
		# EXCEPT when a case is about the engine's verdict, where the real one is built from the
		# Model's dtype and the capability the box would hand it.
		stub = lambda self, **_kwargs: frappe._dict(placement_errors=list(errors))  # noqa: E731
		if model_dtype := box.get("model_dtype"):
			stub = _real_engine(model_dtype)
		with patch.object(FakeDeployment, "engine_for", stub):
			return dep._rejection(
				reported,
				# The cards a filter left, named — `fitting_gpus` returns docnames. Taken from the
				# claim unless a case is about the two disagreeing.
				box.get("free", tuple(card["name"] for card in reported.free_gpus)),
				box.get("box_architecture", "amd64"),
				box.get("image_architecture", "amd64"),
			)

	def test_a_box_that_fits_is_not_rejected(self):
		self.assertEqual(self.reject(), "")

	def test_the_wrong_architecture_names_both_sides(self):
		reason = self.reject(box_architecture="arm64", image_architecture="amd64")
		self.assertIn("arm64", reason)
		self.assertIn("amd64", reason)

	def test_a_box_with_no_architecture_recorded_is_on_prem_and_skips_the_check(self):
		# Same reading `_validate_engine_architecture` takes — blank means nothing to check
		# against, not "exclude me".
		self.assertEqual(self.reject(box_architecture=""), "")
		self.assertEqual(self.reject(box_architecture=None), "")

	def test_an_unpinned_replica_makes_the_box_unusable(self):
		# THE one that corrupts silently. A replica with no GPU rows is legal and means unpinned
		# single-GPU; it claims nothing, so every card here READS free while some are busy.
		# Placing on it double-books VRAM, and nothing downstream would notice.
		reason = self.reject(claim=claim(free=(0, 1, 2, 3), unpinned=1))
		self.assertIn("pin no cards", reason)

	def test_too_few_matching_cards_names_the_counts(self):
		reason = self.reject(claim=claim(free=(0, 1)))
		self.assertIn("2 free", reason)
		self.assertIn("4", reason)

	def test_a_named_gpu_type_appears_in_the_shortfall(self):
		reason = self.reject(deployment(gpu_type="h200"), claim=claim(free=(0,)))
		self.assertIn("h200", reason)

	def test_an_older_card_is_rejected_before_the_play_starts(self):
		# The engine's own check, reached through the scheduler: a T4 box has room for the model
		# and cannot represent it, and finding that out on the box costs a pull and a play.
		reason = self.reject(
			deployment(dtype="auto"),
			claim=claim(free=(0, 1, 2, 3), capability=7.5),
			errors=[],
			model_dtype="bfloat16",
		)
		self.assertIn("7.5", reason)
		self.assertIn("float16", reason)

	def test_the_box_becomes_viable_once_the_dtype_is_set(self):
		# Same box, same cards — the deployment names the remedy the rejection asked for.
		self.assertEqual(
			self.reject(
				deployment(dtype="float16"),
				claim=claim(free=(0, 1, 2, 3), capability=7.5),
				model_dtype="bfloat16",
			),
			"",
		)

	def test_the_weakest_card_decides(self):
		# A mixed box runs at its oldest silicon, the same reading the VRAM check takes.
		mixed = claim(free=(0, 1, 2, 3), capability=9.0)
		mixed.capability_by_card["gpu-2"] = 7.5
		self.assertIn("7.5", self.reject(deployment(dtype="auto"), claim=mixed, model_dtype="bfloat16"))

	def test_the_engines_own_verdict_is_passed_through(self):
		# The fit check runs against the box's REAL card size, which is stricter than the
		# deployment's declared min_vram_gb — that is the point of asking it per candidate.
		self.assertEqual(self.reject(errors=["weights do not fit"]), "weights do not fit")


class TestACandidateMeasuresTheBox(unittest.TestCase):
	def build(self, dep=None, **over):
		dep = dep or deployment()
		dep.streams_weights = over.get("streams_weights", False)
		stub = lambda self, **_kwargs: frappe._dict(placement_errors=[])  # noqa: E731
		# Leases are an in-flight hint from Redis, exercised in test_lease; a candidate built
		# here is measured against committed state alone.
		with (
			patch.object(FakeDeployment, "engine_for", stub),
			patch(f"{MODULE}.lease.leased", lambda gpus: set()),
		):
			return dep._candidate(
				frappe._dict(name="inf3", machine="M-1", region=over.get("region", "ap-south-1")),
				over.get("claim", claim(free=(0, 1, 2, 3, 4, 5), replicas=2)),
				"amd64",
				"amd64",
				over.get("siblings", set()),
				over.get("per_region", {}),
			)

	def test_surplus_is_what_is_left_after_the_shape(self):
		# 6 free, 4 needed.
		self.assertEqual(self.build().surplus, 2)

	def test_a_box_that_cannot_fit_has_negative_surplus_and_a_rejection(self):
		candidate = self.build(claim=claim(free=(0, 1)))
		self.assertEqual(candidate.surplus, -2)
		self.assertFalse(candidate.is_viable)

	def test_a_sibling_on_the_box_means_the_weights_are_local(self):
		self.assertTrue(self.build(siblings={"inf3"}).has_local_weights)
		self.assertFalse(self.build(siblings={"inf9"}).has_local_weights)

	def test_a_streamed_model_is_never_local(self):
		# Weights come from S3 the same way everywhere, so no box is warmer than another and
		# WarmCache must not drag every replica onto one box for nothing.
		self.assertFalse(self.build(siblings={"inf3"}, streams_weights=True).has_local_weights)

	def test_the_region_count_comes_from_the_deployments_own_replicas(self):
		self.assertEqual(self.build(per_region={"ap-south-1": 3}).replicas_in_region, 3)
		self.assertEqual(self.build(per_region={"us-east-1": 3}).replicas_in_region, 0)
