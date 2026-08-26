# Copyright (c) 2026, Grove and contributors
# See license.txt
"""A vendor is only a route when we hold all of it: an address, over TLS, with a credential.

Site-backed — the endpoint is what decides whether a model under this provider can be published at
all, and that is a real insert and a real db write.
"""

import frappe
from frappe.tests import IntegrationTestCase


def provider(name, **fields):
	return frappe.get_doc({"doctype": "Model Provider", "name": name, **fields})


def vendor_model(model_id, provider_name):
	"""A model nobody hosts: no HF Repo anywhere, which is the point."""
	return frappe.get_doc(
		{"doctype": "Model", "model_id": model_id, "provider": provider_name, "modality": "text"}
	)


class TestAnEndpointIsAllOrNothing(IntegrationTestCase):
	def test_a_base_url_without_a_key_is_refused(self):
		# Half a provider would publish a model and 401 every request against it.
		with self.assertRaises(frappe.ValidationError):
			provider("probe-nokey", base_url="https://api.probe.test").insert()

	def test_a_plaintext_base_url_is_refused(self):
		# The API key rides this hop.
		with self.assertRaises(frappe.ValidationError):
			provider("probe-http", base_url="http://api.probe.test", api_key="k").insert()

	def test_a_trailing_slash_is_dropped(self):
		# engine_url is a base the client's path is appended to; two slashes is a 404.
		doc = provider("probe-slash", base_url="https://api.probe.test/", api_key="k").insert()
		self.assertEqual(doc.base_url, "https://api.probe.test")

	def test_our_own_provider_needs_nothing(self):
		self.assertTrue(provider("probe-selfhosted").insert().name)


class TestWhatAVendorModelNeeds(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		"""One provider for the class: the rollback is once at the end, not between tests."""
		super().setUpClass()
		cls.vendor = provider("probe-vendor", base_url="https://api.probe.test", api_key="k").insert()

	def test_it_needs_no_hf_repo(self):
		# There is no engine to start, so there are no weights to name.
		doc = vendor_model("probe-sonnet", self.vendor.name).insert()
		self.assertFalse(doc.hf_repo)

	def test_it_is_published_the_moment_it_is_created(self):
		# Nothing else would ever flip it: no deployment status changes for a model we do not host,
		# so a vendor model that had to wait for one would wait forever.
		doc = vendor_model("probe-published", self.vendor.name).insert()
		self.assertTrue(doc.published)

	def test_the_host_only_operations_refuse_it_by_name(self):
		# The form hides the buttons; these are reachable without one, and the errors underneath
		# are about a missing repo or a missing deployment — true, and no help at all.
		doc = vendor_model("probe-buttons", self.vendor.name).insert()
		for operation in (doc.fetch_architecture, doc.mirror_weights):
			with self.subTest(operation.__name__), self.assertRaises(frappe.ValidationError) as caught:
				operation()
			self.assertIn(self.vendor.name, str(caught.exception))

	def test_one_of_ours_still_needs_a_repo(self):
		with self.assertRaises(frappe.MandatoryError):
			vendor_model("probe-ours", "frappe").insert()


class TestAProviderWithNoEndpointIsNotAVendor(IntegrationTestCase):
	def test_a_model_under_it_is_still_one_of_ours(self):
		# `frappe` is this: a name to be published under, nothing to dial. A model there is hosted,
		# so it owes us a repo like any other.
		ours = provider("probe-nameonly").insert()
		with self.assertRaises(frappe.MandatoryError):
			vendor_model("probe-nameonly-model", ours.name).insert()
