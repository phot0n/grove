"""Archive on the server doctypes, and what a new Inference Server takes from its Network.

Pure unit tests: the doc is a SimpleNamespace and frappe's data calls are stubbed, so these pin the
guards and the filters without a site."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.gateway_server.gateway_server import GatewayServer
from grove.grove.doctype.gateway_state_store.gateway_state_store import GatewayStateStore
from grove.grove.doctype.inference_server.inference_server import InferenceServer
from grove.grove.doctype.ingress_server.ingress_server import IngressServer
from grove.grove.doctype.monitoring_agent.monitoring_agent import MonitoringAgent
from grove.server import Server

LIVE = ("!=", "Terminated")


class Blocked(Exception):
	pass


def throw(message, title=None, **kwargs):
	raise Blocked(message)


def rows(table):
	"""A frappe.get_all stub off {doctype: [names]}, recording the filters it was asked with."""
	asked = {}

	def get_all(doctype, filters=None, pluck=None, **kwargs):
		asked[doctype] = filters
		return list(table.get(doctype, []))

	get_all.asked = asked
	return get_all


class TestWhatBlocksAnArchive(unittest.TestCase):
	def test_an_inference_server_with_a_live_replica(self):
		get_all = rows({"Model Replica": ["r1", "r2"]})
		with patch.object(frappe, "get_all", side_effect=get_all):
			blockers = InferenceServer.archive_blockers.fget(SimpleNamespace(name="inf-1"))
		self.assertEqual(len(blockers), 1)
		self.assertIn("r1, r2", blockers[0])
		# Inactive and Broken still hold the box; only Terminated is gone.
		self.assertEqual(get_all.asked["Model Replica"], {"inference_server": "inf-1", "status": LIVE})

	def test_an_empty_inference_server(self):
		with patch.object(frappe, "get_all", side_effect=rows({})):
			self.assertEqual(InferenceServer.archive_blockers.fget(SimpleNamespace(name="inf-1")), [])

	def test_an_ingress_with_boxes_routed_through_it(self):
		get_all = rows({"Inference Server": ["inf-1"]})
		with patch.object(frappe, "get_all", side_effect=get_all):
			blockers = IngressServer.archive_blockers.fget(SimpleNamespace(name="ing-1"))
		self.assertIn("inf-1", blockers[0])
		self.assertEqual(get_all.asked["Inference Server"], {"ingress": "ing-1", "status": LIVE})

	def test_a_monitoring_agent_with_targets_names_each_kind(self):
		get_all = rows({"Inference Server": ["inf-1"], "Pod": ["pod-1"]})
		with patch.object(frappe, "get_all", side_effect=get_all):
			blockers = MonitoringAgent.archive_blockers.fget(SimpleNamespace(name="ma-1"))
		self.assertEqual(len(blockers), 2)
		self.assertEqual(
			set(get_all.asked), {"Inference Server", "Ingress Server", "Gateway Server", "Pod"}
		)
		for filters in get_all.asked.values():
			self.assertEqual(filters, {"monitoring_agent": "ma-1", "status": LIVE})

	def test_a_store_with_gateways_still_on_it(self):
		get_all = rows({"Gateway Server": ["gw1-ap-south-1"]})
		with patch.object(frappe, "get_all", side_effect=get_all):
			blockers = GatewayStateStore.archive_blockers.fget(SimpleNamespace(name="store1"))
		self.assertIn("gw1-ap-south-1", blockers[0])
		# A Broken gateway still points its agent.env here.
		self.assertEqual(get_all.asked["Gateway Server"], {"state_store": "store1", "status": LIVE})

	def test_an_empty_store(self):
		with patch.object(frappe, "get_all", side_effect=rows({})):
			self.assertEqual(GatewayStateStore.archive_blockers.fget(SimpleNamespace(name="store1")), [])

	def gateway_blockers(self, siblings, served):
		region = SimpleNamespace(gateways=lambda exclude=None: siblings)
		db = frappe._dict(exists=lambda doctype, filters: doctype in served)
		with patch.object(frappe, "get_doc", return_value=region), patch.object(frappe, "db", db):
			return GatewayServer.archive_blockers.fget(SimpleNamespace(name="gw-1", region="ap-south-1"))

	def test_the_last_gateway_of_a_serving_region(self):
		blockers = self.gateway_blockers(siblings=[], served={"Inference Server"})
		self.assertIn("Last gateway in ap-south-1", blockers[0])

	def test_a_gateway_with_a_sibling_left(self):
		self.assertEqual(self.gateway_blockers(siblings=[{"name": "gw-2"}], served={"Inference Server"}), [])

	def test_the_last_gateway_of_an_empty_region(self):
		self.assertEqual(self.gateway_blockers(siblings=[], served=set()), [])


class TestArchive(unittest.TestCase):
	def server(self, blockers=(), status="Active", machine="m-1"):
		doc = SimpleNamespace(
			name="s-1", machine=machine, status=status,
			archive_blockers=list(blockers),
			reload=lambda: None, saved=[],
		)
		doc.save = lambda: doc.saved.append(doc.status)
		return doc

	def machine(self, cloud_provider):
		box = SimpleNamespace(cloud_provider=cloud_provider, terminated=[])
		box.terminate = lambda: box.terminated.append(True)
		return box

	def archive(self, doc, box):
		with (
			patch.object(frappe, "throw", side_effect=throw),
			patch.object(frappe, "msgprint"),
			patch.object(frappe, "get_doc", return_value=box),
		):
			Server.archive(doc)

	def test_a_blocker_refuses_before_the_box_is_touched(self):
		doc, box = self.server(blockers=["still serving"]), self.machine("aws")
		with self.assertRaises(Blocked):
			self.archive(doc, box)
		self.assertEqual(box.terminated, [])
		self.assertEqual(doc.status, "Active")

	def test_a_cloud_box_is_terminated_and_the_row_retired(self):
		doc, box = self.server(), self.machine("aws")
		self.archive(doc, box)
		self.assertEqual(box.terminated, [True])
		self.assertEqual(doc.saved, ["Terminated"])

	def test_the_cascade_already_retired_the_row(self):
		# Machine.terminate saves Active servers as Terminated; reload sees it and nothing is saved twice.
		doc, box = self.server(), self.machine("aws")
		doc.reload = lambda: setattr(doc, "status", "Terminated")
		self.archive(doc, box)
		self.assertEqual(doc.saved, [])

	def test_an_on_prem_box_only_retires_the_row(self):
		doc, box = self.server(), self.machine(cloud_provider=None)
		self.archive(doc, box)
		self.assertEqual(box.terminated, [])
		self.assertEqual(doc.saved, ["Terminated"])


class TestANewInferenceServerTakesTheNetworksSingletons(unittest.TestCase):
	def defaults(self, ingresses, agents, ingress=None, agent=None, standalone=0):
		doc = SimpleNamespace(machine="m-1", ingress=ingress, monitoring_agent=agent, is_standalone=standalone)
		doc.get = lambda field: getattr(doc, field)
		doc.set = lambda field, value: setattr(doc, field, value)
		get_all = rows({"Machine": ["m-1", "m-2"], "Ingress Server": ingresses, "Monitoring Agent": agents})
		db = frappe._dict(get_value=lambda *args, **kwargs: "net-1")
		with patch.object(frappe, "db", db), patch.object(frappe, "get_all", side_effect=get_all):
			InferenceServer.default_to_network_singletons(doc)
		self.asked = get_all.asked
		return doc.ingress, doc.monitoring_agent

	def test_exactly_one_of_each_is_taken(self):
		self.assertEqual(self.defaults(["ing-1"], ["ma-1"]), ("ing-1", "ma-1"))
		# Membership is the Machine's network read live, never the server's mirrored copy.
		self.assertEqual(self.asked["Machine"], {"network": "net-1"})
		self.assertEqual(self.asked["Ingress Server"], {"machine": ("in", ["m-1", "m-2"]), "status": LIVE})

	def test_two_or_none_leave_the_field_blank(self):
		self.assertEqual(self.defaults(["ing-1", "ing-2"], []), (None, None))

	def test_an_operators_pick_is_kept(self):
		self.assertEqual(self.defaults(["ing-1"], ["ma-1"], ingress="ing-9"), ("ing-9", "ma-1"))

	def test_a_standalone_box_takes_no_ingress(self):
		self.assertEqual(self.defaults(["ing-1"], ["ma-1"], standalone=1), (None, "ma-1"))

	def test_a_machine_without_a_network_assigns_nothing(self):
		doc = SimpleNamespace(machine="m-1", ingress=None, monitoring_agent=None)
		db = frappe._dict(get_value=lambda *args, **kwargs: None)
		with (
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_all", side_effect=AssertionError("must not query")),
		):
			InferenceServer.default_to_network_singletons(doc)
		self.assertIsNone(doc.ingress)


class TestEveryServerIsAServer(unittest.TestCase):
	def test_every_one_shares_the_base_and_takes_its_machines_name(self):
		for cls in (InferenceServer, IngressServer, GatewayServer, MonitoringAgent, GatewayStateStore):
			with self.subTest(cls.__name__):
				self.assertTrue(issubclass(cls, Server))
				self.assertEqual(cls.get_generated_name(SimpleNamespace(machine="inf1-ap-south-1")), "inf1-ap-south-1")
