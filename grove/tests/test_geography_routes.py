# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What a gateway in one Geography is given. Routes are built only from what runs inside it, and every
user goes to every gateway carrying the pin the gateway enforces. Pure — the rows are faked."""

import unittest
from unittest.mock import patch

import frappe

from grove import pathway_sync

ZONE = "grove.example.com"
MODELS = [
	{"name": "frappe/qwen3-8b", "model_id": "qwen3-8b", "provider": "frappe", "published": 1},
	{"name": "openai/gpt-5", "model_id": "gpt-5", "provider": "openai", "published": 1},
	{"name": "openai-eu/gpt-5", "model_id": "gpt-5", "provider": "openai-eu", "published": 1},
]
REPLICAS = [
	{"name": "MD-in", "model": "frappe/qwen3-8b", "engine_url": "https://203.0.113.1/e/md-in",
	 "inference_server": "INF-in", "geography": "in"},
	{"name": "MD-eu", "model": "frappe/qwen3-8b", "engine_url": "https://203.0.113.2/e/md-eu",
	 "inference_server": "INF-eu-behind", "geography": "eu"},
]
INGRESSES = [{"name": "eu-i1", "region": "eu-central-1", "geography": "eu"}]
SERVERS = [{"name": "INF-eu-behind", "ingress": "eu-i1"}]
PODS = [{"name": "POD-1", "model": "frappe/qwen3-8b", "engine_url": "http://1.2.3.4:8081"}]
# Every pod serves in this one, set on Grove Settings.
POD_GEOGRAPHY = "in"
PROVIDERS = {
	"frappe": {"geography": None},
	"openai": {"base_url": "https://api.openai.com/v1", "api_key": "in-key", "geography": "in"},
	"openai-eu": {"base_url": "https://eu.api.openai.com/v1", "api_key": "eu-key", "geography": "eu"},
}


def in_geography(rows, filters):
	if "geography" not in (filters or {}):
		return rows
	return [row for row in rows if row.get("geography") == filters["geography"]]


def get_all(doctype, filters=None, pluck=None, **kwargs):
	rows = {
		"Model": MODELS,
		"Model Replica": REPLICAS,
		"Ingress Server": INGRESSES,
		"Inference Server": SERVERS,
		"Pod": PODS,
		"Model Provider": [{"name": name, **fields} for name, fields in PROVIDERS.items()],
	}.get(doctype, [])
	rows = in_geography(rows, filters)
	if pluck:
		return [row[pluck] for row in rows]
	return [frappe._dict(row) for row in rows]


def cached_provider(doctype, name):
	provider = frappe._dict(PROVIDERS[name])
	provider.get_password = lambda *args, **kwargs: provider.api_key
	return provider


def routes(geography):
	with (
		patch.object(frappe, "get_all", side_effect=get_all),
		patch.object(frappe, "get_cached_doc", side_effect=cached_provider),
		patch.object(frappe, "db", frappe._dict(get_value=lambda *args: ZONE, get_single_value=lambda *args: POD_GEOGRAPHY)),
		patch.object(frappe, "get_doc", side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "secret")),
	):
		return pathway_sync._gateway_routes(geography)


class TestRoutesStayInTheirGeography(unittest.TestCase):
	def test_a_replica_elsewhere_is_not_routed_to(self):
		rows = routes("in")["frappe/qwen3-8b"]
		self.assertEqual({row["deployment"] for row in rows}, {"MD-in", "POD-1"})

	def test_pods_are_routed_only_in_the_pod_geography(self):
		self.assertNotIn("POD-1", str(routes("eu")))

	def test_an_ingress_row_comes_only_from_its_own_geography(self):
		self.assertEqual({row["deployment"] for row in routes("eu")["frappe/qwen3-8b"]}, {"eu-i1"})
		self.assertNotIn("eu-i1", str(routes("in")))

	def test_a_vendor_is_dialled_only_where_it_processes(self):
		self.assertEqual(routes("in")["openai/gpt-5"][0]["internal_key"], "in-key")
		self.assertNotIn("openai-eu/gpt-5", routes("in"))
		self.assertEqual(routes("eu")["openai-eu/gpt-5"][0]["engine_url"], "https://eu.api.openai.com/v1")
		self.assertNotIn("openai/gpt-5", routes("eu"))

	def test_a_gateway_with_no_geography_gets_no_routes(self):
		# Fail closed: blank must not match rows that were never given a geography.
		self.assertEqual(routes(""), {})


class TestEveryUserCarriesTheirPin(unittest.TestCase):
	def users(self, rows):
		with (
			patch.object(pathway_sync, "model_rows", return_value={}),
			patch.object(pathway_sync, "group_rows", return_value={}),
			patch.object(frappe, "get_all", return_value=[frappe._dict(row) for row in rows]),
		):
			return {user["name"]: user for user in pathway_sync._effective_users()}

	def test_pinned_and_unpinned_users_both_reach_every_gateway(self):
		users = self.users([
			{"name": "u1", "user": "a@x.test", "rate_limited": 0, "log_payloads": 0, "geography": "eu"},
			{"name": "u2", "user": "b@x.test", "rate_limited": 0, "log_payloads": 0, "geography": None},
		])
		self.assertEqual(users["u1"]["geography"], "eu")
		# Blank, never null: the gateway reads absent and "" as unpinned.
		self.assertEqual(users["u2"]["geography"], "")


class TestOneSnapshotPerGeography(unittest.TestCase):
	def test_each_gateway_gets_its_geographys_snapshot_built_once(self):
		built, pushed = [], []
		geographies = {"gw-in-1": "in", "gw-in-2": "in", "gw-eu-1": "eu"}

		def snapshot(geography):
			built.append(geography)
			return {"geography": geography}

		def sync_target(server_type, name, snapshot, force):
			pushed.append((name, snapshot["geography"]))
			return None

		run = unittest.mock.Mock(results=[])
		run.acquire_lock.return_value = True
		with (
			patch.object(pathway_sync, "_new_run", return_value=run),
			patch.object(pathway_sync, "sync_targets", return_value=[(None, [name]) for name in geographies]),
			patch.object(pathway_sync, "_active_ingresses", return_value=[]),
			patch.object(pathway_sync, "gateway_geography", side_effect=geographies.get),
			patch.object(pathway_sync, "gateway_snapshot", side_effect=snapshot),
			patch.object(pathway_sync, "_sync_target", side_effect=sync_target),
		):
			pathway_sync.sync_projection()
		self.assertEqual(sorted(built), ["eu", "in"])
		self.assertEqual(sorted(pushed), [("gw-eu-1", "eu"), ("gw-in-1", "in"), ("gw-in-2", "in")])


if __name__ == "__main__":
	unittest.main()
