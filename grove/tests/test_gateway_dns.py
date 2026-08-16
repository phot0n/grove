# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The two tiers a gateway is reached through, and the two URLs Grove derives from them. Pure — boto3
is replaced by a fake and the docs are SimpleNamespaces, so no site and no network.

Every shape here is worth pinning because Route53 punishes each of them differently. A latency RRSet
is keyed on (name, type, REGION), so the row that used to sit per BOX at the shared name allowed
exactly one gateway per region — hence the multivalue set below it, where one record is one IP with
one health check and the unhealthy ones drop out of the answer. A wrong SetIdentifier silently
replaces another row instead of adding one; a DELETE that does not repeat the record exactly as
written leaves it in place, a black hole for whatever share of customers resolve to a box that is
gone; and a health check is refused deletion while any record set or calculated check still names it,
which is why teardown order is asserted here rather than discovered live.
"""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.cloud_provider.route53 import (
	HEALTH_CHECK_FAILURES,
	HEALTH_CHECK_INTERVAL,
	REGION_HEALTH_THRESHOLD,
	TTL,
	Route53Client,
	Route53Error,
)
from grove.grove.doctype.gateway_server.gateway_server import GatewayServer

ZONE = "grove.example.com"
GATEWAY_HOST = f"api.{ZONE}"
GROUP = f"ap-south-1.{GATEWAY_HOST}"


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
		self.updated_health_checks = []
		# Every mutating call in the order it was made — teardown order is the assertion.
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

	def update_health_check(self, **kwargs):
		self._maybe_fail("update_health_check")
		self.updated_health_checks.append(kwargs)
		return {}

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
		# An account can hold both. Writing the proxy's address into the private one would
		# resolve for nothing outside the VPC, and look like DNS had simply not propagated.
		fake = FakeRoute53(zones=[{"Id": "/hostedzone/Zpriv", "Name": f"{ZONE}.", "Config": {"PrivateZone": True}}])
		with self.assertRaises(Route53Error):
			client(fake).get_hosted_zone_id(ZONE)

	def test_a_zone_that_is_not_there_is_not_a_silent_no_op(self):
		with self.assertRaises(Route53Error):
			client(FakeRoute53(zones=[])).get_hosted_zone_id(ZONE)

	def test_it_is_looked_up_once_per_client(self):
		# Every write does it, and each lookup is an API round trip against a value that cannot
		# change under a running request.
		fake = FakeRoute53()
		c = client(fake)
		with patch.object(fake, "list_hosted_zones_by_name", wraps=fake.list_hosted_zones_by_name) as listed:
			for _ in range(3):
				c.get_hosted_zone_id(ZONE)
			self.assertEqual(1, listed.call_count)


def gateway_arguments(
	identifier="gw1-ap-south-1", public_ip="203.0.113.7", health_check_id="hc-1", set_name=GROUP
):
	return (ZONE, f"{identifier}.{ZONE}", GATEWAY_HOST, set_name, public_ip, identifier, health_check_id)


def delete_arguments(zone, hostname, _gateway_host, set_name, public_ip, identifier, health_check_id):
	"""The upsert's arguments minus the shared name, which only the reconcile pass needs."""
	return (zone, hostname, set_name, public_ip, identifier, health_check_id)


class TestGatewayRecords(unittest.TestCase):
	def setUp(self):
		self.fake = FakeRoute53()
		self.client = client(self.fake)
		self.arguments = gateway_arguments()

	def test_both_records_go_in_one_batch(self):
		# One call, so a box is never halfway into DNS: reachable by its own name but absent
		# from its region's set, or the reverse.
		self.client.upsert_gateway_records(*self.arguments)
		[batch] = self.fake.batches
		self.assertEqual(batch["HostedZoneId"], "Z123")
		self.assertEqual(
			set(rows(batch)), {(f"gw1-ap-south-1.{ZONE}", None), (GROUP, "gw1-ap-south-1")}
		)

	def test_the_box_name_points_at_the_box_and_routes_no_further(self):
		self.client.upsert_gateway_records(*self.arguments)
		own = rows(self.fake.batches[0])[(f"gw1-ap-south-1.{ZONE}", None)]["ResourceRecordSet"]
		self.assertEqual(own["ResourceRecords"], [{"Value": "203.0.113.7"}])
		self.assertNotIn("SetIdentifier", own)
		self.assertNotIn("Region", own)
		self.assertNotIn("HealthCheckId", own)

	def test_a_gateway_is_one_multivalue_row_with_its_own_health_check(self):
		# The escape from one-health-check-per-record: one record per IP, each checked on its own,
		# and Route53 leaves the unhealthy ones out of the answer.
		self.client.upsert_gateway_records(*self.arguments)
		row = rows(self.fake.batches[0])[(GROUP, "gw1-ap-south-1")]["ResourceRecordSet"]
		self.assertTrue(row["MultiValueAnswer"])
		self.assertEqual(row["HealthCheckId"], "hc-1")
		self.assertEqual(row["ResourceRecords"], [{"Value": "203.0.113.7"}])
		self.assertEqual(row["TTL"], TTL)
		# A multivalue row takes no Region: latency lives one tier up, and a row carrying both is
		# refused outright.
		self.assertNotIn("Region", row)

	def test_two_gateways_in_one_region_are_two_rows_in_one_set(self):
		"""The whole point. Before this they were two rows of a latency set keyed on (name, type,
		region), and AWS refused the second one twenty minutes into a provision."""
		self.client.upsert_gateway_records(*gateway_arguments("gw1-ap-south-1", "203.0.113.7", "hc-1"))
		self.client.upsert_gateway_records(*gateway_arguments("gw2-ap-south-1", "203.0.113.8", "hc-2"))
		first, second = (rows(batch) for batch in self.fake.batches)

		self.assertIn((GROUP, "gw1-ap-south-1"), first)
		self.assertIn((GROUP, "gw2-ap-south-1"), second)
		self.assertNotEqual(
			first[(GROUP, "gw1-ap-south-1")]["ResourceRecordSet"]["HealthCheckId"],
			second[(GROUP, "gw2-ap-south-1")]["ResourceRecordSet"]["HealthCheckId"],
		)

	def test_a_row_without_a_health_check_carries_none_rather_than_a_blank(self):
		# Blank is rejected outright, and the field's absence has a meaning of its own: Route53
		# counts an unchecked row as permanently healthy.
		self.client.upsert_gateway_records(*gateway_arguments(health_check_id=""))
		self.assertNotIn("HealthCheckId", rows(self.fake.batches[0])[(GROUP, "gw1-ap-south-1")]["ResourceRecordSet"])

	def test_a_delete_repeats_what_the_upsert_wrote(self):
		# Route53 matches a DELETE on the whole record set, routing policy and health check included.
		self.client.upsert_gateway_records(*self.arguments)
		self.client.delete_gateway_records(*delete_arguments(*self.arguments))
		created, deleted = (rows(batch) for batch in self.fake.batches)
		for key in created:
			with self.subTest(key):
				self.assertEqual(created[key]["Action"], "UPSERT")
				self.assertEqual(deleted[key]["Action"], "DELETE")
				self.assertEqual(created[key]["ResourceRecordSet"], deleted[key]["ResourceRecordSet"])


class TestTheRowLeftAtTheSharedName(unittest.TestCase):
	"""Before the region tier, a gateway's row sat directly at the shared name as a latency record.
	An UPSERT of the new row does not replace it — different name — so it would stay behind
	answering for a box that has moved, with no health check on it."""

	def legacy(self, set_identifier="gw1-ap-south-1", record_type="A"):
		return {
			"Name": f"{GATEWAY_HOST}.",
			"Type": record_type,
			"TTL": 60,
			"ResourceRecords": [{"Value": "203.0.113.7"}],
			"SetIdentifier": set_identifier,
			"Region": "ap-south-1",
		}

	def deletions(self, existing):
		fake = FakeRoute53(existing=existing)
		client(fake).upsert_gateway_records(*gateway_arguments())
		return [
			change["ResourceRecordSet"]
			for change in fake.batches[0]["ChangeBatch"]["Changes"]
			if change["Action"] == "DELETE"
		]

	def test_it_is_removed_in_the_same_batch_as_the_new_rows(self):
		# Same batch, so there is never a moment where the shared name has no answer at all.
		self.assertEqual([self.legacy()], self.deletions([self.legacy()]))

	def test_it_is_deleted_verbatim(self):
		# Its TTL and Region are whatever it was written with, so it cannot be reconstructed — and a
		# DELETE that does not match leaves it in place.
		[deleted] = self.deletions([self.legacy()])
		self.assertEqual(60, deleted["TTL"])
		self.assertEqual("ap-south-1", deleted["Region"])

	def test_another_gateways_row_is_left_alone(self):
		self.assertEqual([], self.deletions([self.legacy(set_identifier="gw2-ap-south-1")]))

	def test_the_region_tiers_own_row_is_left_alone(self):
		# It lives at the same name. Deleting it would take the region out of the latency set and
		# every customer nearest it with it.
		self.assertEqual([], self.deletions([self.legacy(record_type="CNAME")]))

	def test_a_box_that_never_had_one_deletes_nothing(self):
		self.assertEqual([], self.deletions([]))


class TestTheRegionRow(unittest.TestCase):
	"""One row per region in the latency set, owned by the Region rather than a box: it stands for
	every gateway in it."""

	def row(self, latency_reference="ap-south-1"):
		fake = FakeRoute53()
		client(fake).upsert_region_record(ZONE, GATEWAY_HOST, "ap-south-1", latency_reference, "hc-region")
		[change] = fake.batches[0]["ChangeBatch"]["Changes"]
		return change["ResourceRecordSet"]

	def test_it_points_at_the_regions_multivalue_set(self):
		self.assertEqual(GATEWAY_HOST, self.row()["Name"])
		self.assertEqual([{"Value": GROUP}], self.row()["ResourceRecords"])

	def test_it_is_a_cname_so_it_can_carry_a_health_check_outright(self):
		# An alias with EvaluateTargetHealth is also healthy-if-any, but a child row that is missing
		# a check makes it fail silently OPEN — latency keeps answering with a dead region.
		self.assertEqual("CNAME", self.row()["Type"])
		self.assertEqual("hc-region", self.row()["HealthCheckId"])

	def test_the_set_identifier_is_the_region_and_not_a_box(self):
		# A box's identifier names its row one tier down. Reusing it here would have the two tiers
		# overwrite each other's rows at the shared name.
		self.assertEqual("ap-south-1", self.row()["SetIdentifier"])

	def test_the_latency_reference_is_what_resolvers_are_measured_against(self):
		# Only a reference point, so a region AWS has none of names the nearest one it does have.
		self.assertEqual("ap-southeast-1", self.row(latency_reference="ap-southeast-1")["Region"])

	def test_a_delete_repeats_what_the_upsert_wrote(self):
		fake = FakeRoute53()
		c = client(fake)
		c.upsert_region_record(ZONE, GATEWAY_HOST, "ap-south-1", "ap-south-1", "hc-region")
		c.delete_region_record(ZONE, GATEWAY_HOST, "ap-south-1", "ap-south-1", "hc-region")
		created, deleted = (batch["ChangeBatch"]["Changes"][0] for batch in fake.batches)
		self.assertEqual("UPSERT", created["Action"])
		self.assertEqual("DELETE", deleted["Action"])
		self.assertEqual(created["ResourceRecordSet"], deleted["ResourceRecordSet"])


class TestHealthChecks(unittest.TestCase):
	def setUp(self):
		self.fake = FakeRoute53()
		self.client = client(self.fake)

	def test_a_gateway_is_checked_on_the_path_its_own_binary_answers(self):
		# pathway serves /healthz on the plaintext listener outright rather than redirecting
		# it, so a plain HTTP check reaches the process itself — and reports 503 the moment it
		# cannot serve, which is what takes the box out of the answer.
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

	def test_a_region_is_up_while_any_one_of_its_gateways_is(self):
		# Threshold 1 is the rule. Higher and a region with one box left drops out of latency
		# entirely, which is the outage this tier exists to prevent.
		self.client.create_calculated_health_check("ref-region", ["hc-2", "hc-1"])
		config = self.fake.health_checks[0]["HealthCheckConfig"]
		self.assertEqual("CALCULATED", config["Type"])
		self.assertEqual(["hc-1", "hc-2"], config["ChildHealthChecks"])
		self.assertEqual(REGION_HEALTH_THRESHOLD, config["HealthThreshold"])

	def test_a_region_is_repointed_as_gateways_come_and_go(self):
		self.client.update_calculated_health_check("hc-region", ["hc-2", "hc-1"])
		self.assertEqual(
			{"HealthCheckId": "hc-region", "ChildHealthChecks": ["hc-1", "hc-2"], "HealthThreshold": 1},
			self.fake.updated_health_checks[0],
		)

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


def gateway_doc(fake, health_check_id="hc-1"):
	"""A Gateway Server reduced to what its DNS paths read, carrying the real health-check and set-name
	methods so the call ORDER is exercised — Route53 refuses to delete a check anything still names."""
	settings = SimpleNamespace(fleet_zone=ZONE, gateway_host=GATEWAY_HOST)
	doc = SimpleNamespace(
		name="gw1-ap-south-1",
		hostname=f"gw1-ap-south-1.{ZONE}",
		public_ip="203.0.113.7",
		region="ap-south-1",
		health_check_id=health_check_id,
		caller_reference="ref-1",
		has_dns_records=True,
		dns_client=lambda: (client(fake), settings),
	)
	doc.db_set = lambda field, value, **kwargs: setattr(doc, field, value)
	doc.ensure_health_check = lambda c: GatewayServer.ensure_health_check(doc, c)
	doc.delete_health_check = lambda c: GatewayServer.delete_health_check(doc, c)
	doc.set_name = lambda host: GatewayServer.set_name(doc, host)
	return doc


@contextmanager
def fleet(fake, health_checks=True, latency=True):
	"""Both Grove Settings toggles the DNS paths read, plus the region resync, recorded in call order
	beside the Route53 calls so the ordering rules can be asserted as one sequence."""
	with (
		patch(f"{MODULE}.gateway_health_checks_enabled", return_value=health_checks),
		patch(f"{MODULE}.gateway_latency_routing_enabled", return_value=latency),
		patch(f"{MODULE}.sync_region_dns") as region_sync,
	):
		region_sync.side_effect = lambda *a, **k: fake.calls.append("sync_region_dns")
		yield region_sync


class TestTeardownOrder(unittest.TestCase):
	"""Route53 refuses to delete a health check while a record set or a calculated check still names
	it, so the rows have to come off first. Getting this wrong leaves a paid check behind on every
	terminate, and nothing surfaces it."""

	def remove(self, health_check_id="hc-1", fake=None):
		"""Run the removal against a fake Route53, returning it and the region resync it triggered."""
		fake = fake or FakeRoute53()
		doc = gateway_doc(fake, health_check_id=health_check_id)
		with fleet(fake) as region_sync:
			GatewayServer.remove_dns_records(doc)
		return fake, region_sync

	def test_the_rows_come_off_before_the_check_they_name(self):
		fake, _ = self.remove()
		self.assertEqual(
			["change_resource_record_sets", "sync_region_dns", "delete_health_check"], fake.calls
		)

	def test_the_region_is_recomputed_without_this_box(self):
		# The last gateway out takes the region's latency row and calculated check with it, and
		# on_trash runs while this doc's row is still in the database.
		_, region_sync = self.remove(health_check_id="")
		region_sync.assert_called_once_with("ap-south-1", exclude="gw1-ap-south-1")

	def test_a_row_already_gone_does_not_strand_the_doc(self):
		# Refusing to delete would strand the Gateway Server and, through the link, the Machine
		# underneath it.
		fake = FakeRoute53()
		fake.errors["change_resource_record_sets"] = Route53Error("not found", "InvalidChangeBatch")
		self.remove(fake=fake)
		# And the check still goes, so a terminate does not leave one paid for behind.
		self.assertEqual(["hc-1"], fake.deleted_health_checks)


class TestHealthCheckingTurnedOff(unittest.TestCase):
	"""The development answer: no check per box, and the row resolves unconditionally because Route53
	counts an unchecked record as healthy."""

	def sync(self, health_check_id="", health_checks=False):
		fake = FakeRoute53()
		doc = gateway_doc(fake, health_check_id=health_check_id)
		with fleet(fake, health_checks=health_checks):
			GatewayServer.sync_dns_records(doc)
		return fake, doc

	def test_no_check_is_created_and_the_row_carries_none(self):
		fake, _ = self.sync()
		self.assertEqual([], fake.health_checks)
		row = rows(fake.batches[0])[(GROUP, "gw1-ap-south-1")]["ResourceRecordSet"]
		self.assertNotIn("HealthCheckId", row)

	def test_the_box_is_still_in_its_regions_set(self):
		# Health checking is ejection, not addressing. Off, every box still resolves.
		fake, _ = self.sync()
		self.assertIn((GROUP, "gw1-ap-south-1"), rows(fake.batches[0]))

	def test_a_check_from_before_the_setting_was_turned_off_is_released(self):
		# Only after the row and the region's check have let go — Route53 refuses otherwise — and it
		# would sit billed and watching nothing.
		fake, doc = self.sync(health_check_id="hc-1")
		self.assertEqual(
			["change_resource_record_sets", "sync_region_dns", "delete_health_check"], fake.calls
		)
		self.assertEqual("", doc.health_check_id)

	def test_turned_back_on_the_box_gets_one_before_its_row_names_it(self):
		fake, doc = self.sync(health_checks=True)
		self.assertEqual("hc-1", doc.health_check_id)
		self.assertEqual(
			["create_health_check", "change_resource_record_sets", "sync_region_dns"], fake.calls
		)
		self.assertEqual(
			"hc-1", rows(fake.batches[0])[(GROUP, "gw1-ap-south-1")]["ResourceRecordSet"]["HealthCheckId"]
		)


class TestLatencyRoutingTurnedOff(unittest.TestCase):
	"""Simple mode: no region tier at all. Every gateway's row sits in one multivalue set AT the shared
	name — same record shape, minus the CNAME hop and the AWS region name a latency row has to be
	measured against, which is the part a development fleet cannot supply."""

	def sync(self, existing=None, health_check_id="hc-1", health_checks=True):
		fake = FakeRoute53(existing=existing or [])
		doc = gateway_doc(fake, health_check_id=health_check_id)
		with fleet(fake, health_checks=health_checks, latency=False) as region_sync:
			GatewayServer.sync_dns_records(doc)
		return fake, region_sync

	def latency_row(self, set_identifier="gw1-ap-south-1"):
		"""What the shared name holds on a fleet that was doing latency routing until now: the same
		record set as simple mode's row — name, type and identifier all match — under another policy."""
		return {
			"Name": f"{GATEWAY_HOST}.",
			"Type": "A",
			"TTL": 60,
			"ResourceRecords": [{"Value": "203.0.113.7"}],
			"SetIdentifier": set_identifier,
			"Region": "ap-south-1",
		}

	def test_the_row_goes_straight_to_the_shared_name(self):
		fake, _ = self.sync()
		row = rows(fake.batches[0])[(GATEWAY_HOST, "gw1-ap-south-1")]["ResourceRecordSet"]
		self.assertTrue(row["MultiValueAnswer"])
		self.assertEqual([{"Value": "203.0.113.7"}], row["ResourceRecords"])
		# No Region to name, which is the whole point: a Region that is not an AWS one has no latency
		# reference to give, and simple mode never asks for one.
		self.assertNotIn("Region", row)

	def test_there_is_no_per_region_set(self):
		fake, _ = self.sync()
		self.assertNotIn((GROUP, "gw1-ap-south-1"), rows(fake.batches[0]))

	def test_the_box_still_answers_at_its_own_name(self):
		fake, _ = self.sync()
		self.assertIn((f"gw1-ap-south-1.{ZONE}", None), rows(fake.batches[0]))

	def test_the_region_tier_is_torn_down_before_the_row_is_written(self):
		"""Route53 refuses a CNAME beside a record of any other type, and the region's latency row IS a
		CNAME at the shared name — so it has to be gone before an A record can exist there. Written in
		the other order the sync fails outright."""
		fake, region_sync = self.sync()
		self.assertEqual(["sync_region_dns", "change_resource_record_sets"], fake.calls)
		region_sync.assert_called_once_with("ap-south-1")

	def test_a_latency_row_of_this_box_is_replaced_rather_than_upserted(self):
		"""The same record set under a different routing policy. Route53 will not UPSERT one policy
		into another, so it is deleted in its own change first and written again after."""
		fake, _ = self.sync(existing=[self.latency_row()])
		replace, write = fake.batches
		self.assertEqual(
			[{"Action": "DELETE", "ResourceRecordSet": self.latency_row()}],
			replace["ChangeBatch"]["Changes"],
		)
		self.assertEqual(
			"UPSERT", rows(write)[(GATEWAY_HOST, "gw1-ap-south-1")]["Action"]
		)

	def test_a_row_already_in_the_right_policy_is_left_to_the_upsert(self):
		# Otherwise every sync would delete and recreate the row, leaving the shared name briefly
		# without this box's address for no reason at all.
		already = {**self.latency_row(), "MultiValueAnswer": True}
		already.pop("Region")
		fake, _ = self.sync(existing=[already])
		self.assertEqual(1, len(fake.batches))

	def test_another_boxs_row_is_never_touched(self):
		fake, _ = self.sync(existing=[self.latency_row(set_identifier="gw2-ap-south-1")])
		self.assertEqual(1, len(fake.batches))


class TestSwitchingBackToLatencyRouting(unittest.TestCase):
	def test_the_row_at_the_shared_name_is_removed_in_the_batch_that_replaces_it(self):
		"""Going the other way the two are DIFFERENT record sets — the new one lives a name down — so
		the delete rides along with the writes and the shared name is never without an answer. It has
		to go in this batch regardless: the region's CNAME lands at that name straight after, and
		Route53 refuses a CNAME beside an A record."""
		simple_row = {
			"Name": f"{GATEWAY_HOST}.",
			"Type": "A",
			"TTL": 60,
			"ResourceRecords": [{"Value": "203.0.113.7"}],
			"SetIdentifier": "gw1-ap-south-1",
			"MultiValueAnswer": True,
		}
		fake = FakeRoute53(existing=[simple_row])
		doc = gateway_doc(fake)
		with fleet(fake) as region_sync:
			GatewayServer.sync_dns_records(doc)

		[batch] = fake.batches
		self.assertEqual("DELETE", rows(batch)[(GATEWAY_HOST, "gw1-ap-south-1")]["Action"])
		self.assertEqual("UPSERT", rows(batch)[(GROUP, "gw1-ap-south-1")]["Action"])
		# And the region's CNAME is written only after that batch has removed the A record.
		self.assertEqual(["change_resource_record_sets", "sync_region_dns"], fake.calls)
		region_sync.assert_called_once_with("ap-south-1")


class FakeProxy:
	"""A Gateway Server doc reduced to what the two derivations read, carrying the real property
	so the name and the URL built from it are exercised together."""

	hostname = GatewayServer.hostname

	def __init__(self, name="gw1-ap-south-1", public_ip="203.0.113.7", admin_url=None):
		self.name = name
		self.public_ip = public_ip
		self.admin_url = admin_url


class TestDerivedNames(unittest.TestCase):
	"""hostname and admin_url are derived, never typed. admin_url has to name ONE box — Gateway
	Host deliberately names them all — and https on a name the wildcard covers is what makes
	`requests` verify the certificate without a line of change in agent_sync."""

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
		self.assertEqual(self.hostname(ZONE), f"gw1-ap-south-1.{ZONE}")

	def test_no_zone_means_no_name_at_all(self):
		for blank in ("", None):
			self.assertEqual(self.hostname(blank), "")

	def test_the_admin_api_is_addressed_by_that_name_over_tls(self):
		self.assertEqual(self.admin_url(ZONE), f"https://gw1-ap-south-1.{ZONE}/grove-admin")

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
