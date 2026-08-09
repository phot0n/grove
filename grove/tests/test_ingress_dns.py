# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The name an ingress is reached by. Pure — boto3 is replaced by a fake, so no site and no network.

One record, and the absence of anything else is the point. An ingress is addressed by the gateway's
route table, so it is never part of a set: a SetIdentifier here would silently make the second
ingress REPLACE the first's row rather than sit beside it, and a routing policy on a record nothing
resolves as a group is a way to be wrong for free.
"""

import unittest
from unittest.mock import patch

from grove.cloud_provider.route53 import Route53Client
from grove.tests.test_gateway_dns import ZONE, FakeRoute53, records

HOSTNAME = f"aps1-i1.{ZONE}"


def client(fake):
	with patch("boto3.client", return_value=fake):
		return Route53Client("key", "secret")


class TestIngressRecords(unittest.TestCase):
	def setUp(self):
		self.fake = FakeRoute53()
		self.client = client(self.fake)
		self.arguments = (ZONE, HOSTNAME, "203.0.113.9", "aps1-i1")

	def test_an_ingress_gets_exactly_one_record(self):
		self.client.upsert_ingress_records(*self.arguments)
		[batch] = self.fake.batches
		self.assertEqual(set(records(batch)), {HOSTNAME})

	def test_it_names_this_box_and_routes_no_further(self):
		# A gateway reaches this ingress by this exact name out of its route table, and the
		# control plane pushes a replica table to the same one. Any routing policy here would
		# make a second ingress overwrite this row instead of standing beside it.
		self.client.upsert_ingress_records(*self.arguments)
		own = records(self.fake.batches[0])[HOSTNAME]["ResourceRecordSet"]
		self.assertEqual(own["ResourceRecords"], [{"Value": "203.0.113.9"}])
		for policy in ("SetIdentifier", "MultiValueAnswer", "HealthCheckId", "Region"):
			with self.subTest(policy):
				self.assertNotIn(policy, own)

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


if __name__ == "__main__":
	unittest.main()
