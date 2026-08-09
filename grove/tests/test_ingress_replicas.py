# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The table one ingress is given: every Active replica it OWNS, dialled privately.

Ownership is explicit — an Inference Server names its ingress — and that is what keeps the
capacity gate honest. `inflight:<engine>` lives in each box's own Redis, so a replica counted by
two ingresses is admitted to twice its --max-num-seqs and vLLM starts queueing, silently.

The other rule that matters is what happens to a replica with no private address. It is left OUT,
not dialled publicly, or customer traffic quietly crosses the internet to a box that was supposed
to be private. `test-aws-1` is that case on the fleet today: private_ip blank.
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
		filters = dict(filters or {})
		if doctype == "Inference Server" and "ingress" in filters:
			rows = [r for r in rows if r.get("ingress") == filters["ingress"]]
		if doctype == "Model Deployment":
			wanted = filters.get("inference_server")
			names = list(wanted[1]) if wanted else []
			rows = [r for r in rows if r["status"] == "Active" and r["inference_server"] in names]
		if doctype == "Machine":
			wanted = filters.get("name")
			rows = [r for r in rows if r["name"] in list(wanted[1])] if wanted else rows
		if pluck:
			return [r[pluck] for r in rows]
		return [frappe._dict(r) for r in rows]


MODELS = [{"name": "qwen3-35b"}, {"name": "llama-70b"}]
SERVERS = [
	{"name": "INF-local", "machine": "M-local", "ingress": "ING-1"},
	{"name": "INF-elsewhere", "machine": "M-elsewhere", "ingress": "ING-2"},
	{"name": "INF-noprivate", "machine": "M-noprivate", "ingress": "ING-1"},
	{"name": "INF-direct", "machine": "M-direct", "ingress": None},
]
MACHINES = [
	{"name": "M-local", "network": "Mumbai", "private_ip": "10.0.1.4"},
	{"name": "M-elsewhere", "network": "Mumbai", "private_ip": "10.1.1.4"},
	{"name": "M-noprivate", "network": "Mumbai", "private_ip": ""},
	{"name": "M-direct", "network": "Mumbai", "private_ip": "10.0.1.9"},
]
DEPLOYMENTS = [
	{"name": "MD-1", "model": "qwen3-35b", "engine_url": "https://203.0.113.7/e/md-1",
	 "status": "Active", "inference_server": "INF-local", "max_num_seqs": 8},
	{"name": "MD-2", "model": "llama-70b", "engine_url": "https://203.0.113.8/e/md-2",
	 "status": "Active", "inference_server": "INF-elsewhere", "max_num_seqs": 4},
	{"name": "MD-5", "model": "llama-70b", "engine_url": "https://203.0.113.10/e/md-5",
	 "status": "Active", "inference_server": "INF-direct", "max_num_seqs": 4},
	{"name": "MD-3", "model": "llama-70b", "engine_url": "https://203.0.113.9/e/md-3",
	 "status": "Active", "inference_server": "INF-noprivate", "max_num_seqs": 4},
	{"name": "MD-4", "model": "qwen3-35b", "engine_url": "https://203.0.113.7/e/md-4",
	 "status": "Draft", "inference_server": "INF-local", "max_num_seqs": 8},
]


class TestReplicasForIngress(unittest.TestCase):
	def routes(self, ingress="ING-1"):
		from grove import gateway_sync

		query = FakeQuery(SERVERS, MACHINES, DEPLOYMENTS, MODELS)
		with (
			patch.object(frappe, "get_all", side_effect=query),
			patch.object(
				frappe, "get_doc",
				side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "internal"),
			),
		):
			return gateway_sync._replicas_for_ingress(ingress)

	def test_a_local_replica_is_dialled_privately(self):
		[route] = self.routes()["qwen3-35b"]
		self.assertEqual(route["engine_url"], "https://10.0.1.4/e/md-1")
		self.assertEqual(route["deployment"], "MD-1")
		self.assertEqual(route["capacity"], 8)

	def test_a_row_carries_only_what_the_ingress_reads(self):
		# Every field here is read by pickReplica or handlePick. `server` is not: it is the
		# gateway's request-id part, and the ingress already has the box's address in engine_url.
		[route] = self.routes()["qwen3-35b"]
		self.assertEqual(
			set(route), {"engine_url", "internal_key", "healthy", "capacity", "deployment"}
		)

	def test_a_box_owned_by_another_ingress_is_not_in_this_table(self):
		# Same Network, different owner. If both ingresses held it, each would count only its own
		# half of the traffic and the replica would run at twice its --max-num-seqs.
		urls = [r["engine_url"] for routes in self.routes().values() for r in routes]
		self.assertNotIn("https://10.1.1.4/e/md-2", urls)
		self.assertFalse([u for u in urls if "203.0.113.8" in u])

	def test_a_box_that_names_no_ingress_is_nobodys(self):
		# It stays on the gateway's direct path, so no ingress may claim it.
		for ingress in ("ING-1", "ING-2"):
			with self.subTest(ingress):
				urls = [r["engine_url"] for routes in self.routes(ingress).values() for r in routes]
				self.assertFalse([u for u in urls if "10.0.1.9" in u], urls)

	def test_a_local_replica_with_no_private_address_is_excluded_not_dialled_publicly(self):
		# Fail closed. The model reads unavailable in this network and someone syncs the Machine,
		# rather than customer traffic crossing the internet to a box meant to be private.
		urls = [r["engine_url"] for routes in self.routes().values() for r in routes]
		self.assertFalse([u for u in urls if "203.0.113.9" in u], urls)

	def test_a_model_with_nothing_local_is_not_sent_at_all(self):
		# Not named, rather than named with an empty list. Retiring it is the prune flag's job —
		# see test_the_push_is_marked_complete, which is what makes omitting it safe.
		self.assertNotIn("llama-70b", self.routes())

	def test_only_active_deployments_are_routed(self):
		deployments = [r["deployment"] for routes in self.routes().values() for r in routes]
		self.assertNotIn("MD-4", deployments)

	def test_an_ingress_that_owns_nothing_sends_an_empty_table(self):
		# And prune turns that into "delete everything you hold", which is what retires the last
		# model on an ingress whose boxes were all reassigned.
		self.assertEqual(self.routes(ingress="ING-unused"), {})

	def test_only_the_models_it_can_serve_are_sent(self):
		# The catalogue runs to hundreds and an ingress fronts a handful. Naming every model, one
		# empty list each, was nearly the whole payload on every sync.
		self.assertEqual(list(self.routes()), ["qwen3-35b"])


if __name__ == "__main__":
	unittest.main()


class TestThePushIsMarkedComplete(unittest.TestCase):
	"""Omitting a model is only safe because the push says it is the WHOLE table.

	Without prune the agent upserts what it is given and keeps the rest, so a model whose replicas
	all moved to another ingress would keep a key here pointing at a box this one cannot reach —
	and /pick would hand it out."""

	def test_sync_replicas_sets_prune(self):
		from grove import gateway_sync

		sent = {}
		with (
			patch.object(gateway_sync, "_replicas_for_ingress", return_value={"m": []}),
			patch.object(gateway_sync, "_conn", return_value=(None, "http://x", "t")),
			patch.object(
				gateway_sync, "_post",
				side_effect=lambda url, token, path, payload: sent.update(payload) or {"models": 1},
			),
		):
			gateway_sync.sync_replicas("ING-1")
		self.assertIs(sent.get("prune"), True)


class TestWhichIngressesAPushReaches(unittest.TestCase):
	"""A deployment change moves exactly one ingress's table: the one that OWNS its box.

	Not every ingress, and not every ingress in the Network either — a second Mumbai ingress
	fronting different boxes holds a table this change did not touch, and pushing to it is a
	round trip that rewrites the same bytes.
	"""

	def owners(self, servers, ingresses):
		from grove import gateway_sync

		def get_all(doctype, filters=None, pluck=None, **kwargs):
			filters = dict(filters or {})
			if doctype == "Inference Server":
				wanted = list(filters["name"][1])
				return [s["ingress"] for s in servers if s["name"] in wanted and s.get("ingress")]
			wanted = list(filters["name"][1])
			return [
				i["name"]
				for i in ingresses
				if i["name"] in wanted and i["status"] == "Active" and i.get("network")
			]

		with patch.object(frappe, "get_all", side_effect=get_all):
			return gateway_sync.owning_ingresses([s["name"] for s in servers])

	def test_only_the_owner_is_pushed_to(self):
		owners = self.owners(
			[{"name": "INF-a", "ingress": "ing-1"}],
			[{"name": "ing-1", "status": "Active", "network": "Mumbai"},
			 {"name": "ing-2", "status": "Active", "network": "Mumbai"}],
		)
		self.assertEqual(owners, ["ing-1"])

	def test_a_box_with_no_ingress_moves_nothing(self):
		owners = self.owners(
			[{"name": "INF-direct", "ingress": None}],
			[{"name": "ing-1", "status": "Active", "network": "Mumbai"}],
		)
		self.assertEqual(owners, [])

	def test_an_ingress_that_cannot_take_a_push_is_skipped(self):
		# Broken, or configured without a Network — a scheduled run must not die over one box.
		owners = self.owners(
			[{"name": "INF-a", "ingress": "ing-broken"}, {"name": "INF-b", "ingress": "ing-nonet"}],
			[{"name": "ing-broken", "status": "Broken", "network": "Mumbai"},
			 {"name": "ing-nonet", "status": "Active", "network": None}],
		)
		self.assertEqual(owners, [])
