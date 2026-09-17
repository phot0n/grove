# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The multivalue set a gateway is reached through, and the two URLs Grove derives from it. Pure —
boto3 is replaced by a fake and the docs are SimpleNamespaces, so no site and no network.

Every shape here is worth pinning because Route53 punishes each of them differently. One record is
one IP with one health check, so the unhealthy ones drop out of the answer. A wrong SetIdentifier
silently replaces another row instead of adding one; a DELETE that does not repeat the record exactly
as written leaves it in place, a black hole for whatever share of customers resolve to a box that is
gone; and a health check is refused deletion while a record still names it, which is why teardown
order is asserted here rather than discovered live.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.cloud_provider.dns import (
	HEALTH_CHECK_FAILURES,
	HEALTH_CHECK_INTERVAL,
	TTL,
	Route53Client,
	Route53Error,
)
from grove.grove.doctype.gateway_server.gateway_server import GatewayServer

ZONE = "grove.example.com"
GATEWAY_HOST = f"api.{ZONE}"


class FakeRoute53:
	"""Stands in for the boto3 client. Records every change batch and every health-check call, in
	order, and answers the zone and record listings from canned lists."""

	def __init__(self, zones=None, existing=None):
		self.zones = zones if zones is not None else [
			{"Id": "/hostedzone/Z123", "Name": f"{ZONE}.", "Config": {"PrivateZone": False}},
		]
		self.existing = existing or []
		self.batches = []
		self.health_checks = []
		self.deleted_health_checks = []
		# Every mutating call in order; teardown order is the assertion.
		self.calls = []
		self.errors = {}

	def _maybe_fail(self, operation):
		self.calls.append(operation)
		if error := self.errors.get(operation):
			raise error

	def list_hosted_zones_by_name(self, **kwargs):
		return {"HostedZones": self.zones}

	def list_resource_record_sets(self, **kwargs):
		return {"ResourceRecordSets": self.existing}

	def change_resource_record_sets(self, **kwargs):
		self._maybe_fail("change_resource_record_sets")
		self.batches.append(kwargs)
		return {"ChangeInfo": {"Id": "/change/C1"}}

	def create_health_check(self, **kwargs):
		self._maybe_fail("create_health_check")
		self.health_checks.append(kwargs)
		return {"HealthCheck": {"Id": f"hc-{len(self.health_checks)}"}}

	def delete_health_check(self, **kwargs):
		self._maybe_fail("delete_health_check")
		self.deleted_health_checks.append(kwargs["HealthCheckId"])
		return {}

	def get_paginator(self, _operation):
		return SimpleNamespace(paginate=lambda: [{"HealthChecks": self.existing_health_checks}])

	existing_health_checks = []


def client(fake):
	with patch("boto3.client", return_value=fake):
		return Route53Client("key", "secret")


def rows(batch):
	"""Every change in a batch, keyed by (name, set identifier) — one gateway's row and another's
	live at the same name and differ only by identifier.

	The name is stripped of its trailing dot: a record Grove wrote has none and one Route53 listed
	back always does, and a change batch can carry both."""
	return {
		(
			change["ResourceRecordSet"]["Name"].rstrip("."),
			change["ResourceRecordSet"].get("SetIdentifier"),
		): change
		for change in batch["ChangeBatch"]["Changes"]
	}


class TestHostedZoneLookup(unittest.TestCase):
	def test_it_finds_the_public_zone_by_name(self):
		self.assertEqual(client(FakeRoute53()).get_hosted_zone_id(ZONE), "Z123")

	def test_a_private_zone_of_the_same_name_is_not_it(self):
		# An account can hold both, and writing into the private one resolves for nothing outside
		# the VPC — which reads as DNS not having propagated.
		fake = FakeRoute53(zones=[{"Id": "/hostedzone/Zpriv", "Name": f"{ZONE}.", "Config": {"PrivateZone": True}}])
		with self.assertRaises(Route53Error):
			client(fake).get_hosted_zone_id(ZONE)

	def test_a_zone_that_is_not_there_is_not_a_silent_no_op(self):
		with self.assertRaises(Route53Error):
			client(FakeRoute53(zones=[])).get_hosted_zone_id(ZONE)

	def test_it_is_looked_up_once_per_client(self):
		# Every write does it, against a value that cannot change under a running request.
		fake = FakeRoute53()
		c = client(fake)
		with patch.object(fake, "list_hosted_zones_by_name", wraps=fake.list_hosted_zones_by_name) as listed:
			for _ in range(3):
				c.get_hosted_zone_id(ZONE)
			self.assertEqual(1, listed.call_count)


def gateway_arguments(identifier="gw1-ap-south-1", public_ip="203.0.113.7", health_check_id="hc-1"):
	return (ZONE, f"{identifier}.{ZONE}", GATEWAY_HOST, public_ip, identifier, health_check_id)


class TestGatewayRecords(unittest.TestCase):
	def setUp(self):
		self.fake = FakeRoute53()
		self.client = client(self.fake)
		self.arguments = gateway_arguments()

	def test_both_records_go_in_one_batch(self):
		# One call, so a box is never halfway in: named but absent from its set, or the reverse.
		self.client.upsert_gateway_records(*self.arguments)
		[batch] = self.fake.batches
		self.assertEqual(batch["HostedZoneId"], "Z123")
		self.assertEqual(
			set(rows(batch)), {(f"gw1-ap-south-1.{ZONE}", None), (GATEWAY_HOST, "gw1-ap-south-1")}
		)

	def test_the_box_name_points_at_the_box_and_routes_no_further(self):
		self.client.upsert_gateway_records(*self.arguments)
		own = rows(self.fake.batches[0])[(f"gw1-ap-south-1.{ZONE}", None)]["ResourceRecordSet"]
		self.assertEqual(own["ResourceRecords"], [{"Value": "203.0.113.7"}])
		self.assertNotIn("SetIdentifier", own)
		self.assertNotIn("Region", own)
		self.assertNotIn("HealthCheckId", own)

	def test_a_gateway_is_one_multivalue_row_with_its_own_health_check(self):
		# The escape from one-health-check-per-record: one record per IP, each checked on its own.
		self.client.upsert_gateway_records(*self.arguments)
		row = rows(self.fake.batches[0])[(GATEWAY_HOST, "gw1-ap-south-1")]["ResourceRecordSet"]
		self.assertTrue(row["MultiValueAnswer"])
		self.assertEqual(row["HealthCheckId"], "hc-1")
		self.assertEqual(row["ResourceRecords"], [{"Value": "203.0.113.7"}])
		self.assertEqual(row["TTL"], TTL)
		# No latency policy: a row carrying both is refused outright.
		self.assertNotIn("Region", row)

	def test_two_gateways_are_two_rows_in_one_set(self):
		"""The whole point: rows of a latency set are keyed on (name, type, region), and AWS refused the
		second gateway twenty minutes into a provision."""
		self.client.upsert_gateway_records(*gateway_arguments("gw1-ap-south-1", "203.0.113.7", "hc-1"))
		self.client.upsert_gateway_records(*gateway_arguments("gw2-ap-south-1", "203.0.113.8", "hc-2"))
		first, second = (rows(batch) for batch in self.fake.batches)

		self.assertIn((GATEWAY_HOST, "gw1-ap-south-1"), first)
		self.assertIn((GATEWAY_HOST, "gw2-ap-south-1"), second)
		self.assertNotEqual(
			first[(GATEWAY_HOST, "gw1-ap-south-1")]["ResourceRecordSet"]["HealthCheckId"],
			second[(GATEWAY_HOST, "gw2-ap-south-1")]["ResourceRecordSet"]["HealthCheckId"],
		)

	def test_a_row_without_a_health_check_carries_none_rather_than_a_blank(self):
		# Blank is rejected outright, and absence means something: Route53 counts an unchecked row
		# as permanently healthy.
		self.client.upsert_gateway_records(*gateway_arguments(health_check_id=""))
		self.assertNotIn("HealthCheckId", rows(self.fake.batches[0])[(GATEWAY_HOST, "gw1-ap-south-1")]["ResourceRecordSet"])

	def test_a_delete_repeats_what_the_upsert_wrote(self):
		# Route53 matches a DELETE on the whole record set, routing policy and health check included.
		self.client.upsert_gateway_records(*self.arguments)
		self.client.delete_gateway_records(*self.arguments)
		created, deleted = (rows(batch) for batch in self.fake.batches)
		for key in created:
			with self.subTest(key):
				self.assertEqual(created[key]["Action"], "UPSERT")
				self.assertEqual(deleted[key]["Action"], "DELETE")
				self.assertEqual(created[key]["ResourceRecordSet"], deleted[key]["ResourceRecordSet"])


class TestARowUnderAnotherPolicy(unittest.TestCase):
	"""What the shared name holds for a box that was written as a latency row: the same record set —
	name, type and identifier all match — under another routing policy. Route53 will not UPSERT one
	policy into another, so it is deleted in its own change first and written again after."""

	def latency_row(self, set_identifier="gw1-ap-south-1"):
		return {
			"Name": f"{GATEWAY_HOST}.",
			"Type": "A",
			"TTL": 60,
			"ResourceRecords": [{"Value": "203.0.113.7"}],
			"SetIdentifier": set_identifier,
			"Region": "ap-south-1",
		}

	def upsert(self, existing):
		fake = FakeRoute53(existing=existing)
		client(fake).upsert_gateway_records(*gateway_arguments())
		return fake

	def test_it_is_replaced_rather_than_upserted(self):
		replace, write = self.upsert([self.latency_row()]).batches
		# Deleted verbatim: its TTL and Region are whatever it was written with, and a DELETE that
		# does not match leaves it in place.
		self.assertEqual([{"Action": "DELETE", "ResourceRecordSet": self.latency_row()}], replace["ChangeBatch"]["Changes"])
		self.assertEqual("UPSERT", rows(write)[(GATEWAY_HOST, "gw1-ap-south-1")]["Action"])

	def test_a_row_already_in_the_right_policy_is_left_to_the_upsert(self):
		# Otherwise every sync would delete and recreate the row, leaving the shared name briefly
		# without this box's address for no reason at all.
		already = {**self.latency_row(), "MultiValueAnswer": True}
		already.pop("Region")
		self.assertEqual(1, len(self.upsert([already]).batches))

	def test_another_boxs_row_is_never_touched(self):
		self.assertEqual(1, len(self.upsert([self.latency_row(set_identifier="gw2-ap-south-1")]).batches))

	def test_a_box_that_never_had_one_deletes_nothing(self):
		self.assertEqual(1, len(self.upsert([]).batches))


class TestHealthChecks(unittest.TestCase):
	def setUp(self):
		self.fake = FakeRoute53()
		self.client = client(self.fake)

	def test_a_gateway_is_checked_on_the_path_its_own_binary_answers(self):
		# pathway serves /healthz on the plaintext listener rather than redirecting, so an HTTP
		# check reaches the process itself and reports 503 the moment it cannot serve.
		self.client.create_endpoint_health_check("203.0.113.7", f"gw1-ap-south-1.{ZONE}", "ref-1")
		config = self.fake.health_checks[0]["HealthCheckConfig"]
		self.assertEqual("HTTP", config["Type"])
		self.assertEqual(80, config["Port"])
		self.assertEqual("/healthz", config["ResourcePath"])
		self.assertEqual("203.0.113.7", config["IPAddress"])
		# So the probe arrives with a Host header the gateway knows as its own name.
		self.assertEqual(f"gw1-ap-south-1.{ZONE}", config["FullyQualifiedDomainName"])

	def test_the_failover_window_is_the_two_constants(self):
		self.client.create_endpoint_health_check("203.0.113.7", "", "ref-1")
		config = self.fake.health_checks[0]["HealthCheckConfig"]
		self.assertEqual(HEALTH_CHECK_INTERVAL, config["RequestInterval"])
		self.assertEqual(HEALTH_CHECK_FAILURES, config["FailureThreshold"])

	def test_a_check_already_gone_does_not_block_a_teardown(self):
		self.fake.errors["delete_health_check"] = Route53Error("gone", "NoSuchHealthCheck")
		self.client.delete_health_check("hc-1")  # must not raise

	def test_any_other_delete_failure_is_surfaced(self):
		self.fake.errors["delete_health_check"] = Route53Error("still referenced", "HealthCheckInUse")
		with self.assertRaises(Route53Error):
			self.client.delete_health_check("hc-1")

	def test_a_check_orphaned_mid_create_is_recovered_rather_than_duplicated(self):
		"""A crash between the create and the db_set that remembers the id leaves a check nothing
		names, costing money and answering to nobody — and the retry would fail forever on the
		duplicate caller reference."""
		self.fake.errors["create_health_check"] = Route53Error("exists", "HealthCheckAlreadyExists")
		self.fake.existing_health_checks = [{"Id": "hc-orphan", "CallerReference": "ref-1"}]
		self.assertEqual("hc-orphan", self.client.create_endpoint_health_check("203.0.113.7", "", "ref-1"))


MODULE = "grove.grove.doctype.gateway_server.gateway_server"


def geography_endpoint(doctype, name, field):
	"""The shared name is the gateway's Geography's endpoint, not a fleet setting."""
	assert (doctype, name, field) == ("Geography", "in", "endpoint")
	return GATEWAY_HOST


def gateway_doc(fake, health_check_id="hc-1"):
	"""A Gateway Server reduced to what its DNS paths read, carrying the real health-check methods so
	the call ORDER is exercised — Route53 refuses to delete a check a record still names."""
	doc = SimpleNamespace(
		name="gw1-ap-south-1",
		geography="in",
		fleet_zone=ZONE,
		hostname=f"gw1-ap-south-1.{ZONE}",
		public_ip="203.0.113.7",
		health_check_id=health_check_id,
		caller_reference="ref-1",
		has_dns_records=True,
		dns_client=lambda: client(fake),
	)
	doc.db_set = lambda field, value, **kwargs: setattr(doc, field, value)
	doc.ensure_health_check = lambda c: GatewayServer.ensure_health_check(doc, c)
	doc.delete_health_check = lambda c: GatewayServer.delete_health_check(doc, c)
	return doc


class TestTeardownOrder(unittest.TestCase):
	"""Route53 refuses to delete a health check while a record still names it, so the rows have to come
	off first. Getting this wrong leaves a paid check behind on every terminate, and nothing surfaces
	it."""

	def remove(self, health_check_id="hc-1", fake=None):
		fake = fake or FakeRoute53()
		with patch.object(frappe, "db", SimpleNamespace(get_value=geography_endpoint)):
			GatewayServer.remove_dns_records(gateway_doc(fake, health_check_id=health_check_id))
		return fake

	def test_the_rows_come_off_before_the_check_they_name(self):
		self.assertEqual(["change_resource_record_sets", "delete_health_check"], self.remove().calls)

	def test_a_row_already_gone_does_not_strand_the_doc(self):
		# Refusing to delete would strand the Gateway Server and the Machine under it.
		fake = FakeRoute53()
		fake.errors["change_resource_record_sets"] = Route53Error("not found", "InvalidChangeBatch")
		self.remove(fake=fake)
		# And the check still goes, so a terminate does not leave one paid for behind.
		self.assertEqual(["hc-1"], fake.deleted_health_checks)


class TestSyncingABox(unittest.TestCase):
	def sync(self, health_check_id=""):
		fake = FakeRoute53()
		doc = gateway_doc(fake, health_check_id=health_check_id)
		with patch.object(frappe, "db", SimpleNamespace(get_value=geography_endpoint)):
			GatewayServer.sync_dns_records(doc)
		return fake, doc

	def test_a_box_gets_its_check_before_its_row_names_it(self):
		fake, doc = self.sync()
		self.assertEqual("hc-1", doc.health_check_id)
		self.assertEqual(["create_health_check", "change_resource_record_sets"], fake.calls)
		self.assertEqual("hc-1", rows(fake.batches[0])[(GATEWAY_HOST, "gw1-ap-south-1")]["ResourceRecordSet"]["HealthCheckId"])

	def test_a_check_it_already_has_is_reused(self):
		fake, _ = self.sync(health_check_id="hc-7")
		self.assertEqual(["change_resource_record_sets"], fake.calls)

	def test_the_box_joins_its_geographys_endpoint(self):
		fake, _ = self.sync()
		self.assertIn((GATEWAY_HOST, "gw1-ap-south-1"), rows(fake.batches[0]))

	def test_the_box_also_answers_at_its_own_name(self):
		fake, _ = self.sync()
		self.assertIn((f"gw1-ap-south-1.{ZONE}", None), rows(fake.batches[0]))


class FakeProxy:
	"""A Gateway Server doc reduced to what the two derivations read, carrying the real property
	so the name and the URL built from it are exercised together."""

	hostname = GatewayServer.hostname
	fleet_zone = GatewayServer.fleet_zone
	short_name = GatewayServer.short_name

	def __init__(self, name="gw1-ap-south-1", public_ip="203.0.113.7", admin_url=None, geography="in"):
		self.name = name
		self.geography = geography
		self.public_ip = public_ip
		self.admin_url = admin_url


class TestDerivedNames(unittest.TestCase):
	"""hostname and admin_url are derived, never typed. admin_url has to name ONE box — Gateway
	Host deliberately names them all — and https on a name the wildcard covers is what makes
	`requests` verify the certificate without a line of change in pathway_sync."""

	def settings(self, zone):
		"""The box's Geography's zone. frappe.db is a Local proxy with no site bound, so the attribute
		is replaced whole."""
		def get_value(doctype, name, field):
			assert (doctype, name, field) == ("Geography", "in", "fleet_zone")
			return zone
		return patch.object(frappe, "db", SimpleNamespace(get_value=get_value))

	def hostname(self, zone):
		with self.settings(zone):
			return FakeProxy().hostname

	def admin_url(self, zone, public_ip="203.0.113.7"):
		with self.settings(zone):
			doc = FakeProxy(public_ip=public_ip)
			GatewayServer.set_admin_url(doc)
			return doc.admin_url

	def test_a_box_is_named_under_the_zone(self):
		self.assertEqual(self.hostname(ZONE), f"gw1-ap-south-1.{ZONE}")

	def test_no_zone_means_no_name_at_all(self):
		for blank in ("", None):
			self.assertEqual(self.hostname(blank), "")

	def test_the_admin_api_is_addressed_by_that_name_over_tls(self):
		self.assertEqual(self.admin_url(ZONE), f"https://gw1-ap-south-1.{ZONE}/grove-admin")

	def test_a_box_named_with_its_domain_does_not_repeat_it(self):
		with self.settings(ZONE):
			self.assertEqual(FakeProxy(name=f"gw2-ap-south-1.{ZONE}").hostname, f"gw2-ap-south-1.{ZONE}")

	def test_a_box_with_no_geography_has_no_name(self):
		with self.settings(ZONE):
			self.assertEqual(FakeProxy(geography=None).hostname, "")

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
