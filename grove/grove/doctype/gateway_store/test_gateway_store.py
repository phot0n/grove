# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""One store per Network, and what a gateway is told about it. Pure: frappe's data calls are
stubbed, so no site."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.gateway_server.gateway_server import GatewayServer
from grove.grove.doctype.gateway_store.gateway_store import GatewayStore


class Refused(Exception):
	pass


def stub_db(machines):
	"""frappe.db off {machine: {field: value}}."""
	return SimpleNamespace(get_value=lambda doctype, name, field: machines.get(name, {}).get(field))


def stub_get_all(stores):
	"""frappe.get_all off {network: [(store, status)]}, the Machine named after its store."""

	def get_all(doctype, filters=None, pluck=None, **kwargs):
		if doctype == "Machine":
			return [store for store, _ in stores.get(filters["network"], [])]
		wanted = filters["status"]  # "Active", or ("!=", "Terminated")
		matches = lambda status: status != wanted[1] if isinstance(wanted, tuple) else status == wanted  # noqa: E731
		return [
			store for pairs in stores.values() for store, status in pairs
			if store in filters["machine"][1] and matches(status)
		]

	return get_all


class TestOneStorePerNetwork(unittest.TestCase):
	def validate(self, name, stores, status="Pending", network="Mumbai"):
		doc = SimpleNamespace(name=name, machine=name, status=status)
		with (
			patch.object(frappe, "db", stub_db({name: {"network": network}})),
			patch.object(frappe, "get_all", side_effect=stub_get_all(stores)),
			patch.object(frappe, "throw", side_effect=Refused),
		):
			GatewayStore.validate_one_per_network(doc)

	def test_the_first_store_of_a_network_is_accepted(self):
		self.validate("store1", {"Mumbai": [("store1", "Pending")]})

	def test_a_second_live_store_is_refused(self):
		# Two stores split the gateways onto two counters, which is the over-admission a store removes.
		with self.assertRaises(Refused):
			self.validate("store2", {"Mumbai": [("store1", "Active"), ("store2", "Pending")]})

	def test_a_terminated_store_leaves_room_for_its_replacement(self):
		self.validate("store2", {"Mumbai": [("store1", "Terminated"), ("store2", "Pending")]})

	def test_a_box_in_no_network_serves_no_gateways(self):
		with self.assertRaises(Refused):
			self.validate("store1", {}, network=None)


class TestWhatAGatewayIsGiven(unittest.TestCase):
	def test_a_store_is_dialled_at_its_private_address_with_its_password(self):
		store = SimpleNamespace(listen_ip="10.0.61.9", get_password=lambda field: "pw")
		self.assertEqual(
			GatewayStore.redis_variables.fget(store),
			{"redis_addr": "10.0.61.9:6379", "redis_password": "pw"},
		)

	def test_the_address_is_the_machines_read_live(self):
		with patch.object(frappe, "db", stub_db({"store1": {"private_ip": "10.0.61.9"}})):
			self.assertEqual(GatewayStore.listen_ip.fget(SimpleNamespace(machine="store1")), "10.0.61.9")

	def test_a_machine_with_no_private_address_is_refused(self):
		store = SimpleNamespace(machine="store1")
		with (
			patch.object(frappe, "db", stub_db({"store1": {}})),
			patch.object(frappe, "throw", side_effect=Refused),
			self.assertRaises(Refused),
		):
			GatewayStore.listen_ip.fget(store)

	def network_store(self, stores, network="Mumbai"):
		with (
			patch.object(frappe, "db", stub_db({"gw1": {"network": network}})),
			patch.object(frappe, "get_all", side_effect=stub_get_all(stores)),
			patch.object(frappe, "throw", side_effect=Refused),
		):
			return GatewayServer.network_store.fget(SimpleNamespace(name="gw1", machine="gw1"))

	def test_a_gateway_takes_its_networks_active_store(self):
		self.assertEqual(self.network_store({"Mumbai": [("store1", "Active")]}), "store1")

	def test_a_store_not_yet_active_refuses_the_gateway(self):
		# Setup has not finished: there is no Redis there to run on.
		with self.assertRaises(Refused):
			self.network_store({"Mumbai": [("store1", "Installing")]})

	def test_a_gateway_in_no_network_is_refused(self):
		with self.assertRaises(Refused):
			self.network_store({"Mumbai": [("store1", "Active")]}, network=None)


if __name__ == "__main__":
	unittest.main()
