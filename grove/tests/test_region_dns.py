# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The tier a Region owns: the calculated health check that says whether the region is up at all, and
the latency row that sends nearby clients to it.

It lives here rather than on a box because one row stands for every gateway in the region — the first
gateway in creates the pair and the last one out removes it. Two things make that worth pinning. The
child list is what makes the latency tier honest: without a check whose children are the region's
gateways, latency keeps answering with a region whose boxes have all died, and a child that is
MISSING a check counts as healthy, so a box with none must be left out rather than passed as blank.
And Route53 refuses to delete a check while a record set still names it, so the row has to come off
first or every emptied region leaves a paid check behind.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.region.region import Region, dns_label
from grove.tests.test_gateway_dns import GATEWAY_HOST, ZONE, FakeRoute53, client


class FakeRegion:
	"""A Region reduced to what the tier logic reads, carrying the real methods so the child list and
	the call order are exercised together."""

	dns_label = Region.dns_label
	caller_reference = Region.caller_reference
	sync_gateway_dns = Region.sync_gateway_dns
	gateways = Region.gateways
	ensure_health_check = Region.ensure_health_check
	delete_health_check = Region.delete_health_check
	remove_gateway_dns = Region.remove_gateway_dns

	def __init__(self, name="ap-south-1", latency_reference="ap-south-1", health_check_id=""):
		self.name = name
		self.latency_reference = latency_reference
		self.health_check_id = health_check_id

	def db_set(self, field, value, **kwargs):
		setattr(self, field, value)


def sync(region, checks, exclude=None, dns=True, health_checks=True, latency=True):
	"""Run the region's sync against a fake Route53 and return it, plus the filters it queried with.
	`checks` is one entry per gateway still in the region — its health check id, or blank."""
	fake = FakeRoute53()
	settings = SimpleNamespace(
		fleet_zone=ZONE if dns else "",
		gateway_host=GATEWAY_HOST if dns else "",
		dns_provider="AWS" if dns else "",
	)
	settings.get = lambda field: getattr(settings, field, "")
	rows = [frappe._dict(name=f"gw{i}-ap-south-1", health_check_id=c) for i, c in enumerate(checks)]
	with (
		patch("frappe.get_single", return_value=settings),
		patch("frappe.get_all", return_value=rows) as queried,
		patch("grove.grove.doctype.region.region.dns_credentials", return_value=("key", "secret")),
		patch("grove.grove.doctype.region.region.Route53Client", return_value=client(fake)),
		patch(
			"grove.grove.doctype.region.region.gateway_health_checks_enabled",
			return_value=health_checks,
		),
		patch(
			"grove.grove.doctype.region.region.gateway_latency_routing_enabled",
			return_value=latency,
		),
		patch("frappe.throw", side_effect=frappe.ValidationError),
	):
		region.sync_gateway_dns(exclude=exclude)
	return fake, queried


def latency_row(fake):
	[change] = fake.batches[0]["ChangeBatch"]["Changes"]
	return change


class TestTheFirstGatewayInARegion(unittest.TestCase):
	def setUp(self):
		self.region = FakeRegion()
		self.fake, _ = sync(self.region, ["hc-1"])

	def test_it_creates_the_regions_calculated_check_over_itself(self):
		config = self.fake.health_checks[0]["HealthCheckConfig"]
		self.assertEqual("CALCULATED", config["Type"])
		self.assertEqual(["hc-1"], config["ChildHealthChecks"])

	def test_the_check_is_remembered_on_the_region(self):
		# Not on a box: the next gateway in has to find it rather than create a second one.
		self.assertEqual("hc-1", self.region.health_check_id)

	def test_the_latency_row_is_written_naming_that_check(self):
		row = latency_row(self.fake)["ResourceRecordSet"]
		self.assertEqual("UPSERT", latency_row(self.fake)["Action"])
		self.assertEqual(GATEWAY_HOST, row["Name"])
		self.assertEqual(self.region.health_check_id, row["HealthCheckId"])

	def test_the_check_exists_before_the_row_that_names_it(self):
		self.assertEqual(["create_health_check", "change_resource_record_sets"], self.fake.calls)


class TestTheGatewaysBehindOneRegion(unittest.TestCase):
	def test_a_second_gateway_repoints_the_check_rather_than_making_another(self):
		# Two calculated checks for one region would each be half blind, and the latency row can name
		# only one of them.
		fake, _ = sync(FakeRegion(health_check_id="hc-region"), ["hc-1", "hc-2"])
		self.assertEqual([], fake.health_checks)
		self.assertEqual(["hc-1", "hc-2"], fake.updated_health_checks[0]["ChildHealthChecks"])

	def test_a_gateway_without_a_check_of_its_own_is_not_a_child(self):
		# As a child it would count as permanently healthy, which would hold the whole region up
		# while every box in it was dead.
		fake, _ = sync(FakeRegion(health_check_id="hc-region"), ["hc-1", "", None])
		self.assertEqual(["hc-1"], fake.updated_health_checks[0]["ChildHealthChecks"])

	def test_only_the_regions_own_live_gateways_are_counted(self):
		_, queried = sync(FakeRegion(health_check_id="hc-region"), ["hc-1"])
		filters = queried.call_args.kwargs["filters"]
		self.assertEqual("ap-south-1", filters["region"])
		# A terminated box's records come off on the way out, so its check is not a child either.
		self.assertEqual(("!=", "Terminated"), filters["status"])

	def test_the_box_being_deleted_is_excluded_by_name(self):
		# on_trash runs while the doc's row is still in the database, so it would otherwise count
		# itself and the region would never be torn down.
		_, queried = sync(FakeRegion(health_check_id="hc-region"), ["hc-1"], exclude="gw1-ap-south-1")
		self.assertEqual(("!=", "gw1-ap-south-1"), queried.call_args.kwargs["filters"]["name"])


class TestTheLastGatewayOut(unittest.TestCase):
	def setUp(self):
		self.region = FakeRegion(health_check_id="hc-region")
		self.fake, _ = sync(self.region, [])

	def test_the_region_leaves_the_latency_set(self):
		# Left behind it is a black hole: latency would keep answering with a region that has no
		# gateway at all, and the row's own check has nothing left to watch.
		self.assertEqual("DELETE", latency_row(self.fake)["Action"])

	def test_the_row_comes_off_before_the_check_it_names(self):
		# The other order is refused by Route53, which leaves a paid check behind on every region
		# that empties out — and nothing surfaces it.
		self.assertEqual(["change_resource_record_sets", "delete_health_check"], self.fake.calls)
		self.assertEqual(["hc-region"], self.fake.deleted_health_checks)

	def test_the_region_forgets_the_check(self):
		self.assertEqual("", self.region.health_check_id)

	def test_a_region_that_never_had_a_check_still_loses_its_row(self):
		# With health checking off a region has a row and no check, so reading a missing check as
		# "nothing to remove" would leave that row pointing at an empty multivalue set — an answer
		# with no address in it for everyone nearest this region.
		fake, _ = sync(FakeRegion(), [])
		self.assertEqual(["change_resource_record_sets"], fake.calls)
		self.assertEqual("DELETE", latency_row(fake)["Action"])


class TestHealthCheckingTurnedOff(unittest.TestCase):
	"""The development answer. Both tiers still resolve — Route53 counts a record with no check as
	healthy — so a one-box fleet behind a real zone works and nothing is billed per check."""

	def test_the_region_still_gets_its_latency_row(self):
		"""The trap: the region used to decide "is any gateway left" from the child checks, and with
		health checking off there are none — so it would have deleted its own row and taken every
		customer nearest this region with it."""
		fake, _ = sync(FakeRegion(), ["hc-1"], health_checks=False)
		self.assertEqual("UPSERT", latency_row(fake)["Action"])
		self.assertNotIn("HealthCheckId", latency_row(fake)["ResourceRecordSet"])

	def test_no_calculated_check_is_created(self):
		fake, _ = sync(FakeRegion(), ["hc-1"], health_checks=False)
		self.assertEqual([], fake.health_checks)

	def test_the_check_it_used_to_name_is_released_after_the_row_lets_go(self):
		# Turning the setting off has to give the check back rather than leave it billed and watching
		# nothing — and only once the row no longer names it, or Route53 refuses.
		region = FakeRegion(health_check_id="hc-region")
		fake, _ = sync(region, ["hc-1"], health_checks=False)
		self.assertEqual(["change_resource_record_sets", "delete_health_check"], fake.calls)
		self.assertEqual(["hc-region"], fake.deleted_health_checks)
		self.assertEqual("", region.health_check_id)


class TestARegionWhoseGatewaysHaveNoChecks(unittest.TestCase):
	def test_the_row_resolves_unconditionally_rather_than_never(self):
		"""A calculated check with no children and a threshold of 1 evaluates UNHEALTHY, so creating
		one here would take the whole region out of the latency set — the opposite of what a fleet
		whose boxes predate health checks wants."""
		fake, _ = sync(FakeRegion(), ["", ""])
		self.assertEqual([], fake.health_checks)
		self.assertEqual("UPSERT", latency_row(fake)["Action"])
		self.assertNotIn("HealthCheckId", latency_row(fake)["ResourceRecordSet"])


class TestTheLatencyReference(unittest.TestCase):
	"""A Region is named with its PROVIDER's own region code, and a latency record is measured
	against an AWS region name. The two are the same string for AWS and nothing like it elsewhere."""

	def test_an_aws_name_is_its_own_reference(self):
		for name in ("ap-south-1", "us-east-1", "eu-central-1", "us-gov-west-1"):
			with self.subTest(name):
				region = FakeRegion(name=name, latency_reference="")
				Region.validate(region)
				self.assertEqual(name, region.latency_reference)

	def test_a_provider_code_that_is_not_one_is_left_for_an_operator(self):
		# RunPod's EU-RO-1 is not an AWS region, and Route53 rejects it outright.
		region = FakeRegion(name="EU-RO-1", latency_reference="")
		Region.validate(region)
		self.assertFalse(region.latency_reference)

	def test_one_already_set_is_never_overwritten(self):
		region = FakeRegion(name="ap-south-1", latency_reference="ap-southeast-1")
		Region.validate(region)
		self.assertEqual("ap-southeast-1", region.latency_reference)

	def test_a_region_with_no_reference_is_refused_before_aws_refuses_it(self):
		# Said here, where it names the field to fix, rather than as an AWS InvalidChangeBatch at the
		# end of a provision.
		with self.assertRaises(frappe.ValidationError):
			sync(FakeRegion(name="EU-RO-1", latency_reference=""), ["hc-1"])


class TestTheRegionsDnsLabel(unittest.TestCase):
	def test_a_provider_code_is_slugged_into_a_label(self):
		# It is a DNS label in the middle of a name, so uppercase and dots cannot survive.
		self.assertEqual("eu-ro-1", dns_label("EU-RO-1"))
		self.assertEqual("ap-south-1", dns_label("ap-south-1"))

	def test_no_region_is_no_label(self):
		self.assertEqual("", dns_label(None))


class TestAFleetWithNoDnsConfigured(unittest.TestCase):
	def test_nothing_is_written_at_all(self):
		# The pre-TLS setup: no zone means no records anywhere, so a region has no tier to own.
		fake, _ = sync(FakeRegion(), ["hc-1"], dns=False)
		self.assertEqual([], fake.calls)


if __name__ == "__main__":
	unittest.main()
