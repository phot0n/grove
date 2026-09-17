# Copyright (c) 2026, Grove and contributors
# See license.txt
"""A Geography's endpoint is a name the fleet certificate covers, and one DNS set names it. Pure — the
docs are stubs, so no site."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove.grove.doctype.geography.geography import Geography
from grove.grove.doctype.machine.machine import Machine
from grove.grove.doctype.model_provider.model_provider import ModelProvider
from grove.grove.doctype.region.region import Region
from grove.server import Server

ZONE = "grove.example.com"


def make_test_geography():
	"""The Geography site-backed tests put their regions and vendors in."""
	if not frappe.db.exists("Geography", "test"):
		geography = {"doctype": "Geography", "__newname": "test", "endpoint": "api.test.grove.localhost"}
		frappe.get_doc(geography).insert(ignore_permissions=True)
	return "test"


def validate(endpoint, zone=ZONE, before=None, gateways=()):
	"""Run both name checks on a stub; `before` is the saved doc, as get_doc_before_save returns it."""
	doc = SimpleNamespace(endpoint=endpoint, fleet_zone=zone, get_doc_before_save=lambda: before, gateways=lambda: list(gateways))
	doc.get = lambda field: getattr(doc, field)
	doc.set = lambda field, value: setattr(doc, field, value)
	with patch("frappe.throw", side_effect=frappe.ValidationError):
		Geography.validate_names(doc)
		Geography.validate_fixed_names(doc)
	return doc


def saved(endpoint="eu.grove.example.com", zone=ZONE):
	return frappe._dict(endpoint=endpoint, fleet_zone=zone)


class TestItsNames(unittest.TestCase):
	def test_one_label_under_its_zone_passes(self):
		doc = validate(" eu.grove.example.com ", zone=" grove.example.com ")
		self.assertEqual((doc.endpoint, doc.fleet_zone), ("eu.grove.example.com", "grove.example.com"))

	def test_a_url_is_not_a_hostname(self):
		for endpoint in ("https://eu.grove.example.com", "eu.grove.example.com/v1", "eu.grove.example.com:443", ""):
			with self.subTest(endpoint), self.assertRaises(frappe.ValidationError):
				validate(endpoint)
		with self.assertRaises(frappe.ValidationError):
			validate("eu.grove.example.com", zone="https://grove.example.com")

	def test_a_name_its_wildcard_cannot_cover_is_refused(self):
		for endpoint in ("grove.example.com", "api.eu.grove.example.com", "eu.other.example.com"):
			with self.subTest(endpoint), self.assertRaises(frappe.ValidationError):
				validate(endpoint)

	def test_with_no_zone_any_hostname_passes(self):
		# No certificate yet: a geography that has not moved to TLS.
		validate("api.eu.grove.example.com", zone="")

	def test_neither_moves_while_a_gateway_answers_at_it(self):
		# Its DNS rows and its own name sit in the old values.
		live = ["gw1-eu-central-1"]
		for before, endpoint, zone in (
			(saved(), "eu2.grove.example.com", ZONE),
			(saved(endpoint="eu.old.example.com", zone="old.example.com"), "eu.grove.example.com", ZONE),
		):
			with self.subTest(before), self.assertRaises(frappe.ValidationError):
				validate(endpoint, zone=zone, before=before, gateways=live)
		validate("eu2.grove.example.com", before=saved())

	def test_a_first_zone_is_how_a_live_geography_moves_to_tls(self):
		validate("eu.grove.example.com", before=saved(zone=None), gateways=["gw1-eu-central-1"])


class TestTlsVariables(unittest.TestCase):
	"""What every box here is handed. The key is read through get_password, not off the field: it is
	stored encrypted, and the raw column holds ciphertext nginx cannot load."""

	def variables(self, **fields):
		doc = SimpleNamespace(**{
			"fleet_zone": ZONE, "fleet_tls_cert": "cert-pem",
			"get_password": lambda field, raise_exception=True: "key-pem", **fields,
		})
		return Geography.tls_variables.fget(doc)

	def test_it_carries_the_zone_and_the_certificate(self):
		self.assertEqual(self.variables(), {"fleet_zone": ZONE, "fleet_tls_cert": "cert-pem", "fleet_tls_key": "key-pem"})

	def test_nothing_is_none(self):
		# These land in a Jinja template, where None renders as the string "None" into a server_name.
		blank = self.variables(fleet_zone=None, fleet_tls_cert=None, get_password=lambda field, raise_exception=True: None)
		self.assertEqual(set(blank.values()), {""})


class TestARegionsGeography(unittest.TestCase):
	def validate(self, is_new=False, changed=True, boxes=()):
		doc = SimpleNamespace(
			name="eu-central-1", is_new=lambda: is_new, has_value_changed=lambda field: changed
		)
		with (
			patch.object(frappe, "db", SimpleNamespace(exists=lambda doctype, filters: doctype in boxes)),
			patch("frappe.throw", side_effect=frappe.ValidationError),
		):
			Region.validate(doc)

	def test_it_is_fixed_once_a_box_is_in_the_region(self):
		# Every box copied the old value on its last save; moving the region would leave them lying.
		for doctype in ("Network", "Machine"):
			with self.subTest(doctype), self.assertRaises(frappe.ValidationError):
				self.validate(boxes=[doctype])

	def test_an_empty_region_can_move(self):
		self.validate()
		self.validate(is_new=True, boxes=["Machine"])


class TestWhoCarriesAGeography(unittest.TestCase):
	def test_a_machine_takes_its_regions_after_the_network_sets_it(self):
		# Link fetching runs before validate, so fetch_from would read the region from before the save.
		values = {("Network", "net-1", "region"): "eu-central-1", ("Region", "eu-central-1", "geography"): "eu"}
		doc = SimpleNamespace(network="net-1", region=None, geography=None)
		doc.set_region_from_network = lambda: Machine.set_region_from_network(doc)
		with patch.object(frappe, "db", SimpleNamespace(get_value=lambda *key: values[key])):
			Machine.validate(doc)
		self.assertEqual(("eu-central-1", "eu"), (doc.region, doc.geography))

	def test_a_vendor_must_name_where_it_processes(self):
		def validate(**fields):
			doc = SimpleNamespace(**{"name": "openai-eu", "base_url": None, "anthropic_base_url": None, "geography": None, **fields})
			with patch("frappe.throw", side_effect=frappe.ValidationError):
				ModelProvider.validate(doc)

		with self.assertRaises(frappe.ValidationError):
			validate(anthropic_base_url="https://bedrock-runtime.eu-central-1.amazonaws.com/anthropic")
		validate(base_url="https://eu.api.openai.com/v1", geography="eu")
		validate()  # our own engines take theirs from wherever they run


class TestAServerNamedWithItsDomain(unittest.TestCase):
	def insert(self, name, zone=ZONE):
		doc = SimpleNamespace(doctype="Gateway Server", name=name, geography="in")
		doc.short_name = Server.short_name.fget(doc)
		doc.fleet_zone = zone
		with patch("frappe.throw", side_effect=frappe.ValidationError):
			Server.before_insert(doc)

	def test_a_label_or_a_label_under_its_own_zone_is_accepted(self):
		self.insert("gw2-ap-south-1")
		self.insert(f"gw2-ap-south-1.{ZONE}")

	def test_any_other_domain_is_refused(self):
		# hostname is built from the first label and the zone, so a name elsewhere would lie about it.
		for name, zone in ((f"gw2.other.example.com", ZONE), (f"gw2.{ZONE}", ""), (f"gw_2.{ZONE}", ZONE)):
			with self.subTest(name), self.assertRaises(frappe.ValidationError):
				self.insert(name, zone)


if __name__ == "__main__":
	unittest.main()
