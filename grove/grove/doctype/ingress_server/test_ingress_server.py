# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What an Ingress Server is allowed to hold. Pure — reads the doctype JSON, no site.

Risk 2 of the two-plane split: one binary runs both planes, and the tenant/infra boundary is
enforced by what each side is GIVEN rather than by the code. The doctype carrying no tenant field
is what keeps the control plane from ever pushing keys to an ingress — the moment one grows an
`api_key` or a `grove_user` Link, somebody's keys push will find it.
"""

import json
import unittest
from pathlib import Path

DOCTYPE = Path(__file__).parent / "ingress_server.json"

# Doctypes that only exist to describe a tenant. A Link to any of them on an ingress means the
# infra plane has learned who its callers are.
TENANT_DOCTYPES = {
	"Grove API Key",
	"Grove User",
	"Model",
	"Model Group",
	"Usage Record",
}


class TestAnIngressHoldsNoTenantState(unittest.TestCase):
	def setUp(self):
		self.fields = json.loads(DOCTYPE.read_text())["fields"]

	def test_it_links_to_nothing_tenant_shaped(self):
		for field in self.fields:
			with self.subTest(field["fieldname"]):
				self.assertNotIn(field.get("options"), TENANT_DOCTYPES)

	def test_its_only_secrets_are_the_two_infra_tokens(self):
		# admin_token authenticates a replica-table push; data_token is what a gateway presents on
		# the data path. Both are Grove's own, neither belongs to a tenant, and they are separate
		# on purpose — one token would mean every gateway holding the control plane's credential.
		passwords = [f["fieldname"] for f in self.fields if f["fieldtype"] == "Password"]
		self.assertEqual(sorted(passwords), ["admin_token", "data_token"])

	def test_it_knows_exactly_one_network_and_it_is_its_boxs(self):
		# An ingress reaches its own VPC privately and no other. Mirrored read-only off the Machine,
		# so it cannot disagree with the box — what a validation against the Machine used to check.
		from grove.grove.doctype.ingress_server.ingress_server import IngressServer

		[network] = [field for field in self.fields if field["fieldname"] == "network"]
		self.assertEqual(network["options"], "Network")
		self.assertTrue(network["reqd"])
		self.assertEqual(network["fetch_from"], "machine.network")
		self.assertTrue(network["read_only"])
		self.assertFalse(hasattr(IngressServer, "validate_machine_network"))


if __name__ == "__main__":
	unittest.main()
