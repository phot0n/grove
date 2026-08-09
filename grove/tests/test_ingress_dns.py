# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The names an ingress is reached by. Pure — boto3 is replaced by a fake and the docs are plain
objects, so no site and no network.

The shared name is the whole design: a gateway holds one row per (model, network) and never learns
how many ingresses exist, so adding or removing one is this batch and nothing else. Two things
have to be right for that to hold, and neither fails loudly. Without MultiValueAnswer the second
ingress silently REPLACES the first instead of joining it. Without a health check per record, a
dead ingress keeps being handed out until somebody deletes the record by hand — which is worse
than one healthy ingress, because it fails intermittently.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.cloud_provider.route53 import Route53Client, Route53Error
from grove.grove.doctype.network.network import Network
from grove.tests.test_gateway_dns import ZONE, FakeRoute53, records

INGRESS_HOST = f"mumbai-ingress.{ZONE}"
HOSTNAME = f"aps1-i1.{ZONE}"


class FakeIngressRoute53(FakeRoute53):
	"""The gateway fake plus the health-check API, which is a separate Route53 resource from the
	records that reference it."""

	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.health_checks = []
		self.updates = []
		self.deletes = []

	def create_health_check(self, **kwargs):
		self.health_checks.append(kwargs)
		return {"HealthCheck": {"Id": "HC1"}}

	def update_health_check(self, **kwargs):
		self.updates.append(kwargs)
		return {"HealthCheck": {"Id": kwargs["HealthCheckId"]}}

	def delete_health_check(self, **kwargs):
		self.deletes.append(kwargs)
		return {}


def client(fake):
	with patch("boto3.client", return_value=fake):
		return Route53Client("key", "secret")


class TestIngressRecords(unittest.TestCase):
	def setUp(self):
		self.fake = FakeIngressRoute53()
		self.client = client(self.fake)
		self.arguments = (ZONE, HOSTNAME, INGRESS_HOST, "203.0.113.9", "aps1-i1", "HC1")

	def test_both_records_go_in_one_batch(self):
		self.client.upsert_ingress_records(*self.arguments)
		[batch] = self.fake.batches
		self.assertEqual(set(records(batch)), {HOSTNAME, INGRESS_HOST})

	def test_the_box_name_reaches_this_box_and_no_other(self):
		# /grove-admin hangs off this one: the control plane pushes a replica table to a SPECIFIC
		# ingress, so it must not be part of any shared set.
		self.client.upsert_ingress_records(*self.arguments)
		own = records(self.fake.batches[0])[HOSTNAME]["ResourceRecordSet"]
		self.assertEqual(own["ResourceRecords"], [{"Value": "203.0.113.9"}])
		self.assertNotIn("SetIdentifier", own)
		self.assertNotIn("MultiValueAnswer", own)
		self.assertNotIn("HealthCheckId", own)

	def test_the_shared_name_is_one_row_of_a_multivalue_set(self):
		self.client.upsert_ingress_records(*self.arguments)
		shared = records(self.fake.batches[0])[INGRESS_HOST]["ResourceRecordSet"]
		self.assertTrue(shared["MultiValueAnswer"])
		self.assertEqual(shared["SetIdentifier"], "aps1-i1")
		self.assertEqual(shared["HealthCheckId"], "HC1")

	def test_the_shared_name_is_not_a_latency_set(self):
		# Latency is the gateway fleet's policy — every gateway can serve every client, so nearest
		# wins. Ingresses in one VPC are interchangeable and none is nearer, and a latency set
		# needs a Region the ingress has no meaningful value for.
		self.client.upsert_ingress_records(*self.arguments)
		self.assertNotIn("Region", records(self.fake.batches[0])[INGRESS_HOST]["ResourceRecordSet"])

	def test_an_ingress_with_no_health_check_still_gets_a_record(self):
		# Not a shape anyone should run — it is risk 1 in the plan — but a record naming a check
		# that does not exist is rejected outright, and half a box in DNS is worse.
		self.client.upsert_ingress_records(ZONE, HOSTNAME, INGRESS_HOST, "203.0.113.9", "aps1-i1")
		self.assertNotIn(
			"HealthCheckId", records(self.fake.batches[0])[INGRESS_HOST]["ResourceRecordSet"]
		)

	def test_a_delete_repeats_what_the_upsert_wrote(self):
		# Route53 matches a DELETE on the whole record — routing policy and health check included.
		self.client.upsert_ingress_records(*self.arguments)
		self.client.delete_ingress_records(*self.arguments)
		created, deleted = (records(batch) for batch in self.fake.batches)
		for name in created:
			with self.subTest(name):
				self.assertEqual(created[name]["Action"], "UPSERT")
				self.assertEqual(deleted[name]["Action"], "DELETE")
				self.assertEqual(created[name]["ResourceRecordSet"], deleted[name]["ResourceRecordSet"])


class TestHealthChecks(unittest.TestCase):
	def setUp(self):
		self.fake = FakeIngressRoute53()
		self.client = client(self.fake)

	def test_a_new_ingress_gets_a_check_on_its_own_name_and_address(self):
		self.assertEqual(self.client.sync_health_check("aps1-i1", "203.0.113.9", HOSTNAME), "HC1")
		[created] = self.fake.health_checks
		config = created["HealthCheckConfig"]
		# The address so the check reaches THIS box rather than whichever one the shared name is
		# currently handing out; the name so it sends the SNI and Host header a gateway does.
		self.assertEqual(config["IPAddress"], "203.0.113.9")
		self.assertEqual(config["FullyQualifiedDomainName"], HOSTNAME)
		self.assertEqual((config["Type"], config["Port"], config["ResourcePath"]), ("HTTPS", 443, "/healthz"))

	def test_the_caller_reference_is_the_box_so_a_retry_is_not_a_second_check(self):
		# Route53 returns the existing check for a repeated CallerReference with identical
		# settings. A random one would bill a new check on every re-provision.
		self.client.sync_health_check("aps1-i1", "203.0.113.9", HOSTNAME)
		self.assertEqual(self.fake.health_checks[0]["CallerReference"], "grove-ingress-aps1-i1")

	def test_a_box_that_moved_updates_its_check_instead_of_making_another(self):
		self.assertEqual(
			self.client.sync_health_check("aps1-i1", "198.51.100.4", HOSTNAME, "HC1"), "HC1"
		)
		self.assertEqual(self.fake.health_checks, [])
		self.assertEqual(self.fake.updates[0]["IPAddress"], "198.51.100.4")

	def test_a_check_that_is_already_gone_does_not_block_a_deletion(self):
		def gone(**kwargs):
			raise Route53Error("no such health check", "NoSuchHealthCheck")

		self.fake.delete_health_check = gone
		self.client.delete_health_check("HC1")

	def test_any_other_failure_is_not_swallowed(self):
		def denied(**kwargs):
			raise Route53Error("not authorized", "AccessDenied")

		self.fake.delete_health_check = denied
		with self.assertRaises(Route53Error):
			self.client.delete_health_check("HC1")


class FakeNetwork:
	"""A Network reduced to what the derivation reads, carrying the real property."""

	ingress_host = Network.ingress_host

	def __init__(self, name="Mumbai"):
		self.name = name


class TestIngressHost(unittest.TestCase):
	"""One label under the zone, or the fleet wildcard does not cover it — and an ingress serving
	a name the certificate misses fails as a TLS error inside the gateway, nowhere near here."""

	def zone(self, zone):
		# frappe.db is a Local proxy with no site bound, so the attribute is replaced whole.
		return patch.object(frappe, "db", SimpleNamespace(get_single_value=lambda *args: zone))

	def host(self, network_name="Mumbai", zone=ZONE):
		with self.zone(zone):
			return FakeNetwork(network_name).ingress_host

	def test_a_network_publishes_one_name_for_every_ingress_in_it(self):
		self.assertEqual(self.host(), INGRESS_HOST)

	def test_the_name_is_slugged_so_an_operators_capitals_are_not_a_dns_label(self):
		self.assertEqual(self.host("AP South 1"), f"ap-south-1-ingress.{ZONE}")

	def test_no_zone_means_no_name_at_all(self):
		for blank in ("", None):
			self.assertEqual(self.host(zone=blank), "")

	def test_a_network_name_that_is_not_one_label_is_refused(self):
		# frappe.throw needs a bound site to raise, so the call itself is what is asserted —
		# refusing at all is the behaviour, and the exception type is Frappe's business.
		with self.zone(ZONE), patch.object(frappe, "throw", side_effect=RuntimeError) as thrown:
			with self.assertRaises(RuntimeError):
				FakeNetwork("ap.south").ingress_host
		self.assertIn("more than one", thrown.call_args.args[0])


if __name__ == "__main__":
	unittest.main()
