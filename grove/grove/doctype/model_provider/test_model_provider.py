# Copyright (c) 2026, Grove and contributors
# See license.txt
"""One provider is ours — the one flagged Self Hosted — and every other one is a vendor.

Site-backed — the flag decides whether a model under a provider can be published or deployed at
all, and that is a real insert and a real db write.
"""

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_provider.model_provider import self_hosted_provider


def provider(name, **fields):
	if fields.get("base_url") or fields.get("anthropic_base_url"):
		fields.setdefault("geography", make_test_geography())
	return frappe.get_doc({"doctype": "Model Provider", "name": name, **fields})


def vendor_model(model_id, provider_name):
	"""A model nobody hosts: no HF Repo anywhere, which is the point."""
	return frappe.get_doc(
		{"doctype": "Model", "model_id": model_id, "provider": provider_name, "modality": "text"}
	)


def our_model(model_id):
	"""A model under the Self Hosted provider, named there by leaving the provider blank."""
	return frappe.get_doc(
		{"doctype": "Model", "model_id": model_id, "hf_repo": "probe/Repo", "modality": "text"}
	)


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

	def test_nothing_of_ours_will_deploy_it(self):
		# The form filters it out of the picker; an API insert lands here, on validate's first
		# line, before a missing image or repo gets a turn.
		model = vendor_model("probe-undeployable", self.vendor.name).insert()
		for doc in (
			{"doctype": "Model Deployment", "model": model.name},
			{"doctype": "Pod", "name": "probe-vendor-pod", "model": model.name},
		):
			with self.subTest(doc["doctype"]), self.assertRaises(frappe.ValidationError) as caught:
				frappe.get_doc(doc).insert()
			self.assertIn(self.vendor.name, str(caught.exception))

	def test_one_of_ours_still_needs_a_repo(self):
		with self.assertRaises(frappe.MandatoryError):
			vendor_model("probe-ours", self_hosted_provider()).insert()


class TestTheFlagSaysWhoIsOurs(IntegrationTestCase):
	def test_a_model_under_an_unflagged_provider_is_a_vendors(self):
		# No endpoint yet, but no engine of ours either: it owes no repo and stays dark until the
		# provider can be dialled.
		theirs = provider("probe-unflagged").insert()
		doc = vendor_model("probe-unflagged-model", theirs.name).insert()
		self.assertFalse(doc.published)
		self.assertFalse(doc.provider_is_self_hosted)

	def test_a_model_under_the_flagged_provider_is_ours(self):
		doc = our_model("probe-ours-mirror").insert()
		self.assertEqual(self_hosted_provider(), doc.provider)
		self.assertTrue(doc.provider_is_self_hosted)


class TestOneProviderIsSelfHosted(IntegrationTestCase):
	def test_a_second_one_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			provider("probe-second-ours", is_self_hosted=1).insert()
		self.assertIn(self_hosted_provider(), str(caught.exception))

	def test_it_dials_nothing(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			provider("probe-dialer", is_self_hosted=1, base_url="https://api.probe.test", api_key="k").insert()
		self.assertIn("Self Hosted", str(caught.exception))


class TestWithNoSelfHostedProvider(IntegrationTestCase):
	"""The flag cleared for the class; the rollback puts it back."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Model Provider", self_hosted_provider(), "is_self_hosted", 0)

	def test_a_blank_provider_has_nothing_to_default_to(self):
		with self.assertRaises(frappe.ValidationError):
			our_model("probe-orphan").insert()

	def test_flagging_a_provider_reaches_the_models_under_it(self):
		ours = provider("probe-newly-ours").insert()
		doc = vendor_model("probe-follows-flag", ours.name).insert()
		self.assertFalse(doc.provider_is_self_hosted)
		ours.is_self_hosted = 1
		ours.save()
		self.assertTrue(frappe.db.get_value("Model", doc.name, "provider_is_self_hosted"))
