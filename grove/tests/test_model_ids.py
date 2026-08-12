# Copyright (c) 2026, Grove and contributors
# See license.txt
"""A model id is `<provider>/<name>`, and that is the only id served.

The bare form is deliberately broken rather than kept as an alias: an id that does not say who serves
it is one nobody can read, and two spellings for one model would have to be carried by the route
table, the grant projection AND every engine's --served-model-name to stay consistent.

The models that predated this were renamed in place on the one control-plane site, so there is no
migration to test here — `autoname` runs at insert and is the whole rule.
"""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model.model import DEFAULT_PROVIDER


class TestTheIdIsAlwaysPrefixed(IntegrationTestCase):
	"""Site-backed: autoname is the whole behaviour, so a real insert is the only honest check.
	IntegrationTestCase, not unittest.TestCase — it wraps each test in a transaction and rolls back,
	which is what keeps these probes out of the site."""

	def model(self, display_name, provider=None):
		doc = frappe.get_doc(
			{"doctype": "Model", "display_name": display_name, "modality": "text", "provider": provider}
		)
		doc.insert()
		return doc

	def test_our_own_models_are_named_under_frappe(self):
		doc = self.model("Ponytail Probe 7B")
		self.assertEqual("frappe/ponytail-probe-7b", doc.name)
		self.assertEqual("ponytail-probe-7b", doc.model_id)
		self.assertEqual(DEFAULT_PROVIDER, doc.provider)

	def test_a_third_party_model_is_named_under_its_vendor(self):
		if not frappe.db.exists("Model Provider", "anthropic"):
			frappe.get_doc({"doctype": "Model Provider", "name": "anthropic"}).insert()
		doc = self.model("Claude Sonnet 4.5", provider="anthropic")
		self.assertEqual("anthropic/claude-sonnet-4.5", doc.name)
		self.assertEqual("claude-sonnet-4.5", doc.model_id)

	def test_a_blank_provider_still_gets_a_prefix(self):
		# The prefix IS the id. One model reachable without it would be an id nobody could tell was
		# ours, and the route key would not match what /v1/models advertises.
		doc = self.model("No Provider Named", provider="")
		self.assertTrue(doc.name.startswith(f"{DEFAULT_PROVIDER}/"))

	def test_renaming_is_not_what_display_name_does(self):
		# The id is a client-facing contract: editing the label afterwards must not move it.
		doc = self.model("Before Rename 7B")
		original = doc.name
		doc.display_name = "After Rename 7B"
		doc.save()
		self.assertEqual(original, doc.name)

	def test_a_name_with_nothing_sluggable_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.model("   ")

	def test_a_slash_in_the_display_name_is_refused(self):
		# slugify passes a slash through, and the slash is the provider separator — `Meta/Llama 3`
		# would otherwise name `frappe/meta/llama-3` and read as a provider nobody registered.
		with self.assertRaises(frappe.ValidationError):
			self.model("Meta/Llama 3")


class TestProviderNames(IntegrationTestCase):
	"""The provider name is the prefix of every model id under it, so it has to survive being typed
	into a JSON body by a customer."""

	def test_a_provider_name_must_be_a_slug(self):
		for bad in ("Bad Name Inc", "UPPER", "trailing-", "under_score"):
			with self.subTest(bad), self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Model Provider", "name": bad}).insert()

	def test_a_hyphenated_lowercase_name_is_fine(self):
		doc = frappe.get_doc({"doctype": "Model Provider", "name": "vertex-ai"}).insert()
		self.assertEqual("vertex-ai", doc.name)

	def test_the_default_provider_ships_with_the_app(self):
		# A fixture, so it exists before the first Model is inserted — every Model defaults to it.
		self.assertTrue(frappe.db.exists("Model Provider", DEFAULT_PROVIDER))


if __name__ == "__main__":
	unittest.main()
