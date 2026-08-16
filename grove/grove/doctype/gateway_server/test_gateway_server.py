# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

import json
import pathlib
import unittest
from types import SimpleNamespace

from grove.grove.doctype.gateway_server.gateway_server import GatewayServer

DOCTYPE = pathlib.Path(__file__).parent / "gateway_server.json"


class TestTheAdminTokenIsAlwaysThere(unittest.TestCase):
	"""admin_token is the only credential the control plane has for a gateway.

	It went missing for a while in the worst possible way: the field is read-only, so nobody could
	enter one, and nothing generated one — a Gateway Server was simply created without a token.
	Every push and every provision then raised "Password not found", and a provision does it after
	building the binary, twenty minutes in.
	"""

	def token_for(self, existing=None, field=None):
		"""`existing` is what get_password returns (the real secret); `field` is what the doc's own
		column holds, which for a saved Password field is a row of asterisks rather than the value.

		They are separate on purpose — the two disagreeing is the bug this guards.
		"""
		doc = SimpleNamespace(
			admin_token=field if field is not None else existing,
			set_admin_url=lambda: None,
			get_password=lambda fieldname, **kwargs: existing,
		)
		GatewayServer.set_admin_token(doc)
		return doc.admin_token

	def test_a_new_gateway_gets_one(self):
		self.assertTrue(self.token_for())

	def test_an_existing_blank_one_is_healed(self):
		# In validate rather than before_insert precisely so this works: a gateway created before
		# this generated one is fixed by saving it, not by re-creating it.
		for blank in (None, ""):
			with self.subTest(blank):
				self.assertTrue(self.token_for(blank))

	def test_a_lost_secret_behind_a_placeholder_is_healed(self):
		# The one that got away. A saved Password field leaves a row of asterisks in the doc's own
		# column and the real value in __Auth. Lose the __Auth row — a doctype rename does exactly
		# this — and `if not self.admin_token` reads the asterisks, calls it set, and generates
		# nothing. The doc then looks fine and every push fails.
		healed = self.token_for(existing=None, field="*" * 48)
		self.assertTrue(healed)
		self.assertNotIn("*", healed)

	def test_a_token_that_exists_is_never_replaced(self):
		# Regenerating on every save would silently desync the box, which holds the value it was
		# last given in agent.env — every push would start failing and nothing would say why.
		self.assertEqual("already-set", self.token_for("already-set"))

	def test_two_gateways_do_not_share_one(self):
		self.assertNotEqual(self.token_for(), self.token_for())

	def test_it_is_long_enough_to_be_a_secret(self):
		self.assertGreaterEqual(len(self.token_for()), 32)


class TestTheDoctypeKeepsItsSecretsSecret(unittest.TestCase):
	def setUp(self):
		self.fields = json.loads(DOCTYPE.read_text())["fields"]

	def test_the_admin_token_is_a_password_field(self):
		# Not Data: a Data field would put the control plane's credential in every list view, every
		# report and the doc's own JSON.
		[token] = [f for f in self.fields if f["fieldname"] == "admin_token"]
		self.assertEqual("Password", token["fieldtype"])

	def test_it_is_read_only_so_it_is_generated_and_not_typed(self):
		[token] = [f for f in self.fields if f["fieldname"] == "admin_token"]
		self.assertTrue(token["read_only"])
