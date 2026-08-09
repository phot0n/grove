# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The table one ingress is given: every Active replica inside its Network, dialled privately.

The rule that matters is what happens to a replica with no private address. It is left OUT, not
dialled publicly — otherwise customer traffic quietly crosses the internet to a box that was
supposed to be private, and nothing anywhere reports it. `test-aws-1` is that case on the fleet
today: network = Mumbai, private_ip blank.
"""

import unittest
from unittest.mock import patch

import frappe

from grove.net import private_url


class TestPrivateURL(unittest.TestCase):
	"""Only the host moves. The box's front is on 443 today and moves to 80 at the cutover, so the
	scheme has to come from the URL the control plane already built — one rule that survives the
	move rather than two that disagree during it."""

	def test_the_host_is_swapped_and_nothing_else(self):
		self.assertEqual(
			private_url("https://203.0.113.7/e/md-00007", "10.0.1.4"),
			"https://10.0.1.4/e/md-00007",
		)

	def test_the_scheme_comes_from_the_url_not_from_here(self):
		# After phase 4 the box serves plain http on :80 and engine_url says so. This function is
		# not what decides that, and must not assume either way.
		self.assertEqual(
			private_url("http://203.0.113.7/e/md-00007", "10.0.1.4"),
			"http://10.0.1.4/e/md-00007",
		)

	def test_a_port_on_the_url_is_the_engines_and_stays(self):
		self.assertEqual(
			private_url("http://203.0.113.7:8081/v1", "10.0.1.4"), "http://10.0.1.4:8081/v1"
		)

	def test_no_private_address_means_no_url(self):
		# The caller drops the replica on this. Falling back to the public address here is exactly
		# the failure this design exists to prevent.
		for blank in ("", None):
			self.assertEqual(private_url("https://203.0.113.7/e/md-00007", blank), "")

	def test_a_url_with_no_host_is_not_rewritten_into_one(self):
		for junk in ("", None, "/e/md-00007", "not a url"):
			self.assertEqual(private_url(junk, "10.0.1.4"), "")


class FakeQuery:
	"""Stands in for frappe.get_all over the four doctypes _replicas_for_ingress reads."""

	def __init__(self, servers, machines, deployments, models):
		self.tables = {
			"Inference Server": servers,
			"Machine": machines,
			"Model Deployment": deployments,
			"Model": models,
		}

	def __call__(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		rows = self.tables.get(doctype, [])
		if doctype == "Model Deployment":
			wanted = dict(filters or {}).get("inference_server")
			names = list(wanted[1]) if wanted else []
			rows = [r for r in rows if r["status"] == "Active" and r["inference_server"] in names]
		if doctype == "Machine":
			wanted = dict(filters or {}).get("name")
			rows = [r for r in rows if r["name"] in list(wanted[1])] if wanted else rows
		if pluck:
			return [r[pluck] for r in rows]
		return [frappe._dict(r) for r in rows]


MODELS = [{"name": "qwen3-35b"}, {"name": "llama-70b"}]
SERVERS = [
	{"name": "INF-local", "machine": "M-local"},
	{"name": "INF-elsewhere", "machine": "M-elsewhere"},
	{"name": "INF-noprivate", "machine": "M-noprivate"},
]
MACHINES = [
	{"name": "M-local", "network": "Mumbai", "private_ip": "10.0.1.4"},
	{"name": "M-elsewhere", "network": "Frankfurt", "private_ip": "10.1.1.4"},
	{"name": "M-noprivate", "network": "Mumbai", "private_ip": ""},
]
DEPLOYMENTS = [
	{"name": "MD-1", "model": "qwen3-35b", "engine_url": "https://203.0.113.7/e/md-1",
	 "status": "Active", "inference_server": "INF-local", "max_num_seqs": 8},
	{"name": "MD-2", "model": "llama-70b", "engine_url": "https://203.0.113.8/e/md-2",
	 "status": "Active", "inference_server": "INF-elsewhere", "max_num_seqs": 4},
	{"name": "MD-3", "model": "llama-70b", "engine_url": "https://203.0.113.9/e/md-3",
	 "status": "Active", "inference_server": "INF-noprivate", "max_num_seqs": 4},
	{"name": "MD-4", "model": "qwen3-35b", "engine_url": "https://203.0.113.7/e/md-4",
	 "status": "Draft", "inference_server": "INF-local", "max_num_seqs": 8},
]


class TestReplicasForIngress(unittest.TestCase):
	def routes(self, network="Mumbai"):
		from grove import gateway_sync

		query = FakeQuery(SERVERS, MACHINES, DEPLOYMENTS, MODELS)
		with (
			patch.object(frappe, "get_all", side_effect=query),
			patch.object(frappe, "db", frappe._dict(get_value=lambda *args: network)),
			patch.object(gateway_sync, "frappe", frappe),
			patch.object(
				frappe, "get_doc",
				side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "internal"),
			),
		):
			return gateway_sync._replicas_for_ingress("ING-1")

	def test_a_local_replica_is_dialled_privately(self):
		[route] = self.routes()["qwen3-35b"]
		self.assertEqual(route["engine_url"], "https://10.0.1.4/e/md-1")
		self.assertEqual(route["deployment"], "MD-1")
		self.assertEqual(route["capacity"], 8)

	def test_a_replica_in_another_network_is_not_in_the_table(self):
		# The whole point of the split: a pod restarting in Frankfurt is invisible here.
		urls = [r["engine_url"] for routes in self.routes().values() for r in routes]
		self.assertNotIn("https://10.1.1.4/e/md-2", urls)
		self.assertFalse([u for u in urls if "203.0.113.8" in u])

	def test_a_local_replica_with_no_private_address_is_excluded_not_dialled_publicly(self):
		# Fail closed. The model reads unavailable in this network and someone syncs the Machine,
		# rather than customer traffic crossing the internet to a box meant to be private.
		urls = [r["engine_url"] for routes in self.routes().values() for r in routes]
		self.assertFalse([u for u in urls if "203.0.113.9" in u], urls)

	def test_a_model_with_nothing_local_is_sent_as_empty_not_omitted(self):
		# Empty list → the agent DELs deploy:<model> → /pick answers 503 no-replica. Omitting it
		# would leave a stale table entry routing to a replica this ingress cannot reach.
		self.assertEqual(self.routes()["llama-70b"], [])

	def test_only_active_deployments_are_routed(self):
		deployments = [r["deployment"] for routes in self.routes().values() for r in routes]
		self.assertNotIn("MD-4", deployments)

	def test_an_ingress_in_a_network_with_no_replicas_still_sends_every_model(self):
		# Same reason: every model has to be named so its key is pruned rather than left stale.
		routes = self.routes(network="Singapore")
		self.assertEqual(set(routes), {"qwen3-35b", "llama-70b"})
		self.assertEqual([r for rows in routes.values() for r in rows], [])


if __name__ == "__main__":
	unittest.main()
