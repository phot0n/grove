# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The two names the fleet certificate has to cover. Pure — the Single doc is a stub, so no site.

Neither name is checked by anything downstream. A scheme in either renders an nginx server_name
that matches no request, and a Gateway Host more than one label under the zone is not covered by
`*.<zone>` at all — both reach a customer's SDK as a certificate error, days after the save.
"""

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from grove import tls
from grove.grove.doctype.grove_settings.grove_settings import GroveSettings


class Settings:
	"""Grove Settings reduced to what validate_tls_names touches."""

	def __init__(self, fleet_zone="", gateway_host=""):
		self.fleet_zone = fleet_zone
		self.gateway_host = gateway_host
		self.meta = SimpleNamespace(get_label=lambda field: field)

	def get(self, field):
		return getattr(self, field)

	def set(self, field, value):
		setattr(self, field, value)


def validate(**kwargs):
	settings = Settings(**kwargs)
	GroveSettings.validate_tls_names(settings)
	return settings


class TestTlsNames(unittest.TestCase):
	def test_a_matching_pair_passes(self):
		validate(fleet_zone="grove.example.com", gateway_host="api.grove.example.com")

	def test_neither_set_is_the_pre_tls_fleet(self):
		# Blank is how every proxy provisioned before this ran, and it has to keep saving.
		validate()

	def test_surrounding_whitespace_is_taken_off_rather_than_rejected(self):
		# A pasted hostname is the common case, and a trailing space would otherwise land in a
		# server_name and a certificate request.
		settings = validate(fleet_zone=" grove.example.com ", gateway_host="api.grove.example.com")
		self.assertEqual(settings.fleet_zone, "grove.example.com")

	def test_a_url_is_not_a_hostname(self):
		# frappe.throw needs a bound site, so what is asserted is that the call does not return.
		for host in ("https://api.grove.example.com", "api.grove.example.com/v1", "api.grove.example.com:443"):
			with self.subTest(host), self.assertRaises(Exception):
				validate(fleet_zone="grove.example.com", gateway_host=host)

	def test_a_gateway_host_the_wildcard_cannot_cover_is_refused(self):
		for host in ("grove.example.com", "api.eu.grove.example.com", "api.other.example.com"):
			with self.subTest(host), self.assertRaises(Exception):
				validate(fleet_zone="grove.example.com", gateway_host=host)

	def test_a_gateway_host_alone_is_left_alone(self):
		# No zone means no certificate and no wildcard to be covered by — an IP-or-host fleet
		# that has not migrated yet.
		validate(gateway_host="api.grove.example.com")


class TestLineageStaleness(unittest.TestCase):
	"""Whether certbot is holding a certificate from the other ACME server. It exits 0 and keeps
	what it has when a valid certificate is not due for renewal, so without this check unticking
	Use ACME Staging reports success and leaves the fleet on a certificate no client trusts."""

	def setUp(self):
		self.config = tempfile.mkdtemp()
		os.makedirs(os.path.join(self.config, "renewal"))
		patcher = patch.object(tls, "certbot_dir", lambda kind: self.config)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(shutil.rmtree, self.config, True)

	def write_renewal(self, server):
		path = os.path.join(self.config, "renewal", "grove.example.com.conf")
		with open(path, "w") as handle:
			handle.write(f"[renewalparams]\nserver = {server}\nauthenticator = dns-route53\n")

	def stale(self, acme_staging):
		return tls.is_lineage_stale(SimpleNamespace(acme_staging=acme_staging), "grove.example.com")

	def test_nothing_issued_yet_is_not_stale(self):
		# First run: there is no lineage to disagree with, and --force-renewal would be a
		# duplicate-certificate limit spent on nothing.
		self.assertFalse(self.stale(0))

	def test_a_staging_certificate_is_stale_once_staging_is_off(self):
		self.write_renewal("https://acme-staging-v02.api.letsencrypt.org/directory")
		self.assertTrue(self.stale(0))
		self.assertFalse(self.stale(1))

	def test_a_production_certificate_is_stale_once_staging_is_on(self):
		self.write_renewal("https://acme-v02.api.letsencrypt.org/directory")
		self.assertTrue(self.stale(1))
		self.assertFalse(self.stale(0))


class TestTlsVariables(unittest.TestCase):
	"""What every proxy is handed. The key is read through get_password, not off the field: it
	is stored encrypted, and the raw column holds ciphertext that nginx cannot load."""

	def variables(self, **kwargs):
		settings = SimpleNamespace(
			gateway_host="api.grove.example.com",
			fleet_zone="grove.example.com",
			fleet_tls_cert="cert-pem",
			get_password=lambda field, raise_exception=True: "key-pem",
			**kwargs,
		)
		return GroveSettings.tls_variables.fget(settings)

	def test_it_carries_both_names_and_the_certificate(self):
		self.assertEqual(
			self.variables(),
			{
				"gateway_host": "api.grove.example.com",
				"fleet_zone": "grove.example.com",
				"fleet_tls_cert": "cert-pem",
				"fleet_tls_key": "key-pem",
			},
		)

	def test_nothing_is_none(self):
		# These land in a Jinja template. None would render the string "None" into an nginx
		# server_name, which parses and matches nothing.
		empty = SimpleNamespace(
			gateway_host=None,
			fleet_zone=None,
			fleet_tls_cert=None,
			get_password=lambda field, raise_exception=True: None,
		)
		self.assertEqual(set(GroveSettings.tls_variables.fget(empty).values()), {""})


if __name__ == "__main__":
	unittest.main()
