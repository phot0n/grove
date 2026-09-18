"""A standalone Inference Server: fronted by its own nginx on the fleet name, or refused at Setup
when it has neither that nor an ingress. Pure unit tests over a SimpleNamespace doc."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
import yaml

from grove.grove.doctype.inference_server.inference_server import InferenceServer

FLEET_TLS_DEFAULTS = Path(__file__).parent.parent / "playbooks/roles/fleet_tls/defaults/main.yml"


class Refused(Exception):
	pass


def throw(message, *args, **kwargs):
	raise Refused(message)


def server(**fields):
	doc = SimpleNamespace(
		doctype="Inference Server", name="inf-1", machine="m-1", machine_ip="1.2.3.4",
		ingress=None, is_standalone=0, is_provisioned=0, status="Active", hostname="inf-1.fleet.dev",
		has_fleet_name=True, geography="in",
	)
	doc.__dict__.update(fields)
	return doc


def geography(**fields):
	"""The box's Geography, reduced to the TLS vars it hands out."""
	return SimpleNamespace(tls_variables={"fleet_zone": "fleet.dev", "fleet_tls_cert": "CERT", "fleet_tls_key": "KEY", **fields})


class TestSetupNeedsAFront(unittest.TestCase):
	def setup(self, doc):
		enqueue = MagicMock()
		with (
			patch.object(frappe, "throw", throw),
			patch.object(frappe, "enqueue_doc", enqueue),
			patch.object(frappe, "msgprint"),
		):
			InferenceServer.setup(doc)
		return enqueue

	def test_neither_an_ingress_nor_standalone_is_refused(self):
		with self.assertRaisesRegex(Refused, "tick Standalone"):
			self.setup(server())

	def test_either_front_is_enqueued(self):
		for doc in (server(ingress="ing-1"), server(is_standalone=1)):
			with self.subTest(doc):
				self.setup(doc).assert_called_once()


class TestSaveRefusesBothFronts(unittest.TestCase):
	def test_standalone_with_an_ingress_is_refused(self):
		doc = server(ingress="ing-1", is_standalone=1)
		with patch.object(frappe, "throw", throw), self.assertRaisesRegex(Refused, "Standalone"):
			InferenceServer.validate(doc)


class TestStandaloneIsFixedOnceSetUp(unittest.TestCase):
	def flip(self, before, after):
		after.get_doc_before_save = lambda: before
		with patch.object(frappe, "throw", throw):
			InferenceServer.validate_standalone_is_fixed(after)

	def test_a_set_up_box_keeps_its_choice(self):
		for before, after in (
			(server(is_provisioned=1, is_standalone=1), server(is_provisioned=1)),
			(server(is_provisioned=1), server(is_provisioned=1, is_standalone=1)),
		):
			with self.subTest(before), self.assertRaisesRegex(Refused, "fixed"):
				self.flip(before, after)

	def test_a_box_not_yet_set_up_may_change_it(self):
		self.flip(server(is_standalone=1), server())


class TestAStandaloneBoxIsNotRenamed(unittest.TestCase):
	def test_rename_is_refused(self):
		with patch.object(frappe, "throw", throw), self.assertRaisesRegex(Refused, "DNS record"):
			InferenceServer.before_rename(server(is_standalone=1), "inf-1", "inf2")
		InferenceServer.before_rename(server(), "inf-1", "inf2")


class TestFrontUrl(unittest.TestCase):
	def front_url(self, doc):
		return InferenceServer.front_url.fget(doc)

	def test_a_standalone_box_is_dialled_by_its_fleet_name(self):
		self.assertEqual(self.front_url(server(is_standalone=1)), "https://inf-1.fleet.dev")

	def test_any_other_box_or_one_with_no_published_name_is_dialled_by_ip(self):
		self.assertEqual(self.front_url(server()), "https://1.2.3.4")
		self.assertEqual(self.front_url(server(is_standalone=1, has_fleet_name=False)), "https://1.2.3.4")


class TestTlsVariables(unittest.TestCase):
	def tls_variables(self, doc, zone):
		with patch.object(frappe, "get_doc", return_value=zone):
			return InferenceServer.tls_variables.fget(doc)

	def test_only_a_standalone_box_with_an_issued_cert_gets_the_wildcard(self):
		self.assertEqual(self.tls_variables(server(), geography()), {})
		self.assertEqual(self.tls_variables(server(is_standalone=1), geography(fleet_tls_cert="")), {})
		self.assertEqual(self.tls_variables(server(is_standalone=1, geography=None), geography()), {})

	def test_nginx_is_pointed_at_the_fleet_tls_role_paths(self):
		variables = self.tls_variables(server(is_standalone=1), geography())
		self.assertEqual(variables["fleet_tls_key"], "KEY")
		self.assertFalse(variables["grove_tls_selfsigned"])
		defaults = yaml.safe_load(FLEET_TLS_DEFAULTS.read_text())
		for override, path in (("grove_tls_cert", "fleet_tls_cert_path"), ("grove_tls_key", "fleet_tls_key_path")):
			self.assertEqual(variables[override], "{{ %s }}" % path)
			self.assertIn(path, defaults)


class TestDnsRecordFollowsTheFlag(unittest.TestCase):
	def on_update(self, doc, before, changed=()):
		doc.get_doc_before_save = lambda: before
		doc.has_value_changed = lambda field: field in changed
		doc.sync_dns_records = MagicMock()
		doc.remove_dns_records = MagicMock()
		InferenceServer.on_update(doc)
		return doc.sync_dns_records.called, doc.remove_dns_records.called

	def test_ticking_standalone_writes_the_record(self):
		self.assertEqual(self.on_update(server(is_standalone=1), server()), (True, False))

	def test_unticking_or_terminating_removes_it(self):
		self.assertEqual(self.on_update(server(), server(is_standalone=1)), (False, True))
		terminated = server(is_standalone=1, status="Terminated")
		self.assertEqual(self.on_update(terminated, server(is_standalone=1), ["status"]), (False, True))

	def test_a_new_box_that_is_not_standalone_touches_no_dns(self):
		self.assertEqual(self.on_update(server(), None, ["status"]), (False, False))
