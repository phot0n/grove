# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The names a proxy is reached by: the records Grove writes for them, and the two URLs it
derives from them. Pure — boto3 is replaced by a fake and the doc is a SimpleNamespace, so no
site and no network.

Both records are worth pinning. A latency record set with the wrong SetIdentifier silently
replaces another box's row instead of adding one, and a DELETE that does not repeat the record
exactly as written leaves it in place — a black hole for whatever share of customers resolve to a
box that is gone.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.cloud_provider.route53 import Route53Client, Route53Error
from grove.grove.doctype.gateway_server.gateway_server import GatewayServer

ZONE = "grove.example.com"
GATEWAY_HOST = f"api.{ZONE}"


class FakeRoute53:
	"""Stands in for the boto3 client. Records every change batch so a test can assert what was
	sent, and answers the zone lookup from a canned list."""

	def __init__(self, zones=None):
		self.zones = zones if zones is not None else [
			{"Id": "/hostedzone/Z123", "Name": f"{ZONE}.", "Config": {"PrivateZone": False}},
		]
		self.batches = []

	def list_hosted_zones_by_name(self, **kwargs):
		return {"HostedZones": self.zones}

	def change_resource_record_sets(self, **kwargs):
		self.batches.append(kwargs)
		return {"ChangeInfo": {"Id": "/change/C1"}}


def client(fake):
	with patch("boto3.client", return_value=fake):
		return Route53Client("key", "secret")


def records(batch):
	return {change["ResourceRecordSet"]["Name"]: change for change in batch["ChangeBatch"]["Changes"]}


class TestHostedZoneLookup(unittest.TestCase):
	def test_it_finds_the_public_zone_by_name(self):
		self.assertEqual(client(FakeRoute53()).get_hosted_zone_id(ZONE), "Z123")

	def test_a_private_zone_of_the_same_name_is_not_it(self):
		# An account can hold both. Writing the proxy's address into the private one would
		# resolve for nothing outside the VPC, and look like DNS had simply not propagated.
		fake = FakeRoute53(zones=[{"Id": "/hostedzone/Zpriv", "Name": f"{ZONE}.", "Config": {"PrivateZone": True}}])
		with self.assertRaises(Route53Error):
			client(fake).get_hosted_zone_id(ZONE)

	def test_a_zone_that_is_not_there_is_not_a_silent_no_op(self):
		with self.assertRaises(Route53Error):
			client(FakeRoute53(zones=[])).get_hosted_zone_id(ZONE)


class TestProxyRecords(unittest.TestCase):
	def setUp(self):
		self.fake = FakeRoute53()
		self.client = client(self.fake)
		self.arguments = (ZONE, f"use1-p1.{ZONE}", GATEWAY_HOST, "203.0.113.7", "us-east-1", "use1-p1")

	def test_both_records_go_in_one_batch(self):
		# One call, so a box is never halfway into DNS: reachable by its own name but absent
		# from the fleet set, or the reverse.
		self.client.upsert_gateway_records(*self.arguments)
		[batch] = self.fake.batches
		self.assertEqual(batch["HostedZoneId"], "Z123")
		self.assertEqual(set(records(batch)), {f"use1-p1.{ZONE}", GATEWAY_HOST})

	def test_the_box_name_points_at_the_box_and_routes_no_further(self):
		self.client.upsert_gateway_records(*self.arguments)
		own = records(self.fake.batches[0])[f"use1-p1.{ZONE}"]["ResourceRecordSet"]
		self.assertEqual(own["ResourceRecords"], [{"Value": "203.0.113.7"}])
		self.assertNotIn("SetIdentifier", own)
		self.assertNotIn("Region", own)

	def test_the_fleet_name_is_one_row_of_a_latency_set(self):
		# SetIdentifier is what makes this box's row its own rather than an overwrite of the
		# last one; Region is what the resolver's latency is measured against.
		self.client.upsert_gateway_records(*self.arguments)
		shared = records(self.fake.batches[0])[GATEWAY_HOST]["ResourceRecordSet"]
		self.assertEqual(shared["SetIdentifier"], "use1-p1")
		self.assertEqual(shared["Region"], "us-east-1")
		self.assertEqual(shared["ResourceRecords"], [{"Value": "203.0.113.7"}])

	def test_a_delete_repeats_what_the_upsert_wrote(self):
		# Route53 matches a DELETE on the whole record, routing policy included.
		self.client.upsert_gateway_records(*self.arguments)
		self.client.delete_gateway_records(*self.arguments)
		created, deleted = (records(batch) for batch in self.fake.batches)
		for name in created:
			with self.subTest(name):
				self.assertEqual(created[name]["Action"], "UPSERT")
				self.assertEqual(deleted[name]["Action"], "DELETE")
				self.assertEqual(created[name]["ResourceRecordSet"], deleted[name]["ResourceRecordSet"])


class FakeProxy:
	"""A Gateway Server doc reduced to what the two derivations read, carrying the real property
	so the name and the URL built from it are exercised together."""

	hostname = GatewayServer.hostname

	def __init__(self, name="use1-p1", public_ip="203.0.113.7", admin_url=None):
		self.name = name
		self.public_ip = public_ip
		self.admin_url = admin_url


class TestDerivedNames(unittest.TestCase):
	"""hostname and admin_url are derived, never typed. admin_url has to name ONE box — Gateway
	Host deliberately names them all — and https on a name the wildcard covers is what makes
	`requests` verify the certificate without a line of change in gateway_sync."""

	def settings(self, zone):
		# frappe.db is a Local proxy with no site bound, so the attribute is replaced whole.
		return patch.object(frappe, "db", SimpleNamespace(get_single_value=lambda *args: zone))

	def hostname(self, zone):
		with self.settings(zone):
			return FakeProxy().hostname

	def admin_url(self, zone, public_ip="203.0.113.7"):
		with self.settings(zone):
			doc = FakeProxy(public_ip=public_ip)
			GatewayServer.set_admin_url(doc)
			return doc.admin_url

	def test_a_box_is_named_under_the_zone(self):
		self.assertEqual(self.hostname(ZONE), f"use1-p1.{ZONE}")

	def test_no_zone_means_no_name_at_all(self):
		for blank in ("", None):
			self.assertEqual(self.hostname(blank), "")

	def test_the_admin_api_is_addressed_by_that_name_over_tls(self):
		self.assertEqual(self.admin_url(ZONE), f"https://use1-p1.{ZONE}/grove-admin")

	def test_without_a_zone_it_falls_back_to_the_address(self):
		self.assertEqual(self.admin_url(""), "http://203.0.113.7/grove-admin")

	def test_a_box_with_neither_keeps_whatever_was_set_by_hand(self):
		# Pre-TLS proxies had this typed into the read-only field out of band. Blanking it
		# would take a working gateway off the sync.
		doc = FakeProxy(public_ip=None, admin_url="http://10.0.0.1/grove-admin")
		with self.settings(""):
			GatewayServer.set_admin_url(doc)
		self.assertEqual(doc.admin_url, "http://10.0.0.1/grove-admin")


if __name__ == "__main__":
	unittest.main()
