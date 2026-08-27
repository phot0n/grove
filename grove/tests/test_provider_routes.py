# Copyright (c) 2026, Grove and contributors
# See license.txt
"""A model a third party serves, and what the gateway is told to ask it for.

Two things are being pinned. A vendor-backed model gets a route with no engine behind it — there is
nothing to deploy, so the provider record IS the placement. And `upstream_model` says what goes in
the body: blank for everything we run, because an engine is started under the full Grove id and
rewriting it would ask for a name no engine answers to.

Pure — the rows are faked, no site.
"""

import unittest
from unittest.mock import patch

import frappe

MODELS = [
	# Ours, hosted. The engine answers to frappe/qwen3-8b, so nothing may be rewritten.
	{"name": "frappe/qwen3-8b", "model_id": "qwen3-8b", "provider": "frappe", "published": 1,
	 "modality": "text"},
	# A vendor, taking the id it knows itself by.
	{"name": "anthropic/claude-4-5", "model_id": "claude-4-5", "provider": "anthropic",
	 "published": 1, "upstream_model_id": "claude-sonnet-4-5-20250929", "modality": "text"},
	# A vendor with no override: the bare id is the best guess at its namespace.
	{"name": "anthropic/claude-haiku", "model_id": "claude-haiku", "provider": "anthropic",
	 "published": 1, "modality": "text"},
	# Ours, but the container advertises its own name — the one local case that rewrites.
	{"name": "frappe/nemo-asr", "model_id": "nemo-asr", "provider": "frappe", "published": 1,
	 "upstream_model_id": "test-nemo-asr", "modality": "audio"},
	# A vendor model nothing has published yet: no route at all.
	{"name": "anthropic/claude-draft", "model_id": "claude-draft", "provider": "anthropic",
	 "published": 0, "modality": "text"},
]
PROVIDERS = {
	"frappe": {"base_url": None, "api_version": None, "api_key": ""},
	"anthropic": {"base_url": "https://api.anthropic.com", "api_version": "2023-06-01",
	              "api_key": "vendor-key"},
	# An address with no key is not a route — half a provider dials nothing.
	"halfway": {"base_url": "https://api.halfway.test", "api_version": "", "api_key": ""},
}
DEPLOYMENTS = [
	{"name": "MD-1", "model": "frappe/qwen3-8b", "engine_url": "https://203.0.113.1/e/md-1",
	 "status": "Active", "inference_server": "INF-direct", "max_num_seqs": 4},
]
PODS = [{"name": "POD-1", "model": "frappe/nemo-asr", "engine_url": "http://1.2.3.4:8081",
         "max_num_seqs": 2}]


class FakeQuery:
	def __call__(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		rows = {
			"Model": MODELS,
			"Model Replica": DEPLOYMENTS,
			"Pod": PODS,
			"Model Provider": [{"name": name} for name in PROVIDERS],
			"Inference Server": [{"name": "INF-direct", "ingress": None}],
		}.get(doctype, [])
		if pluck:
			return [r[pluck] for r in rows]
		return [frappe._dict(r) for r in rows]


def fake_cached_doc(doctype, name):
	provider = frappe._dict(PROVIDERS[name])
	provider.get_password = lambda *a, **k: provider.api_key or None
	return provider


def routes():
	from grove import pathway_sync

	with (
		patch.object(frappe, "get_all", side_effect=FakeQuery()),
		patch.object(frappe, "get_cached_doc", side_effect=fake_cached_doc),
		patch.object(frappe, "db", frappe._dict(get_single_value=lambda *args: "")),
		patch.object(
			frappe, "get_doc",
			side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "secret"),
		),
	):
		return pathway_sync._gateway_routes()


class TestAVendorModelIsRoutable(unittest.TestCase):
	def test_it_gets_one_row_naming_the_vendor(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["kind"], "provider")
		self.assertEqual(row["engine_url"], "https://api.anthropic.com")
		self.assertEqual(row["internal_key"], "vendor-key")
		self.assertEqual(row["api_version"], "2023-06-01")

	def test_the_provider_is_the_placement(self):
		# There is no deployment doc to name, and usage has to be attributable to something.
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["deployment"], "anthropic")
		self.assertEqual(row["server"], "anthropic")

	def test_it_claims_no_capacity(self):
		# We divide GPUs we own. A vendor's own 429 is the only cap there is.
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["capacity"], 0)

	def test_an_unpublished_vendor_model_has_no_route(self):
		self.assertNotIn("anthropic/claude-draft", routes())

	def test_an_endpoint_with_no_key_is_not_a_route(self):
		# Half a provider would push a row that 401s every request.
		for rows in routes().values():
			for row in rows:
				self.assertNotIn("halfway", str(row))


class TestWhatTheUpstreamIsAskedFor(unittest.TestCase):
	def test_our_own_engines_are_asked_for_the_id_they_serve(self):
		# The engine's --served-model-name IS the prefixed id. Stripping it would be a 400.
		[row] = routes()["frappe/qwen3-8b"]
		self.assertEqual(row["upstream_model"], "")

	def test_a_vendor_gets_the_exact_string_it_was_given(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["upstream_model"], "claude-sonnet-4-5-20250929")

	def test_a_vendor_without_an_override_gets_the_bare_id(self):
		# Our namespace is not theirs, so the prefix cannot go out.
		[row] = routes()["anthropic/claude-haiku"]
		self.assertEqual(row["upstream_model"], "claude-haiku")

	def test_an_override_reaches_a_local_pod_too(self):
		# A custom image advertises its own name; asking it for the Grove id is a 400.
		[row] = routes()["frappe/nemo-asr"]
		self.assertEqual(row["kind"], "direct")
		self.assertEqual(row["upstream_model"], "test-nemo-asr")
