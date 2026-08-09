# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The ingress's own nginx config, in both shapes it renders. Pure — renders the template, no site
and no box. Shares the gateway's render harness, because the two files have to keep agreeing about
where a certificate comes from.

Same failure mode as the gateway's, one level down and one degree worse: DNS hands out an ingress
at random, so a location under the wrong server block is reachable through a name that means "any
box in this Network". /grove-admin under the shared name would push a replica table to whichever
ingress answered.
"""

import unittest

import yaml

from grove.tests.test_gateway_templates import (
	PLAYBOOKS,
	ZONE,
	environment,
	resolve,
	role_defaults,
	server_with,
)

INGRESS = PLAYBOOKS / "ingress_server"
INGRESS_HOST = f"mumbai-ingress.{ZONE}"
INGRESS_HOSTNAME = f"aps1-i1.{ZONE}"

TEMPLATES = environment(INGRESS)

# What an ingress box has in scope: the roles ingress.yml lists, whose defaults become play vars.
BASE = resolve({
	**role_defaults(PLAYBOOKS / "roles/openresty"),
	**role_defaults(PLAYBOOKS / "roles/grove_https"),
	**role_defaults(PLAYBOOKS / "roles/fleet_tls"),
	"ingress_hostname": "",
	"ingress_host": "",
})

TLS = {
	**BASE,
	"ingress_hostname": INGRESS_HOSTNAME,
	"ingress_host": INGRESS_HOST,
	"fleet_tls_cert": "-----BEGIN CERTIFICATE-----\nnot-a-real-one\n-----END CERTIFICATE-----\n",
	"fleet_tls_key": "-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----\n",
}


def render(variables):
	return TEMPLATES.get_template("nginx.conf.j2").render(**variables)


class TestPlaintextShape(unittest.TestCase):
	"""No Fleet Zone. An ingress provisioned before a certificate exists still has to come up —
	the control plane reaches it over plain http at its address, exactly as a pre-TLS proxy was."""

	def setUp(self):
		self.config = render(BASE)

	def test_it_is_a_whole_config_not_a_conf_d_snippet(self):
		for directive in ("events {", "http {", "worker_processes"):
			with self.subTest(directive):
				self.assertIn(directive, self.config)

	def test_the_admin_api_answers_on_port_eighty(self):
		self.assertIn("location /grove-admin/", server_with(self.config, "listen 80 default_server;"))

	def test_metrics_keep_their_own_tls_listener(self):
		metrics = server_with(self.config, "location = /metrics/node")
		self.assertIn("listen 443 ssl default_server;", metrics)
		self.assertIn("auth_basic_user_file", metrics)

	def test_it_falls_back_to_the_self_signed_box_certificate(self):
		# fleet.crt is not on a box with no zone, and naming it would stop OpenResty starting.
		self.assertIn(BASE["grove_tls_cert"], self.config)
		self.assertNotIn(BASE["fleet_tls_cert_path"], self.config)


class TestTlsShape(unittest.TestCase):
	"""With a Fleet Zone: two names, one wildcard, and the split between them is the point."""

	def setUp(self):
		self.config = render(TLS)
		self.shared = server_with(self.config, f"server_name {INGRESS_HOST};")
		self.box = server_with(self.config, f"server_name {INGRESS_HOSTNAME};")

	def test_the_control_plane_reaches_one_box_and_not_a_random_one(self):
		# The whole reason there are two names. DNS picks an ingress out of the shared set, so a
		# replica-table push sent there would land on whichever box answered that second.
		self.assertIn("location /grove-admin/", self.box)
		self.assertNotIn("location /grove-admin/", self.shared)

	def test_the_scrape_reaches_one_box_too(self):
		self.assertIn("location = /metrics/node", self.box)
		self.assertNotIn("location = /metrics/node", self.shared)

	def test_both_names_answer_the_health_check(self):
		# The Route53 check behind the shared record asks this box by its OWN name, and a gateway
		# checks the shared one. A block missing /healthz drops out of resolution permanently.
		for block in (self.shared, self.box):
			with self.subTest(block=block.splitlines()[1].strip()):
				self.assertIn("location = /healthz", block)

	def test_both_names_present_the_same_wildcard(self):
		for block in (self.shared, self.box):
			with self.subTest(block=block.splitlines()[1].strip()):
				self.assertIn(f"ssl_certificate     {TLS['fleet_tls_cert_path']};", block)
				self.assertIn(f"ssl_certificate_key {TLS['fleet_tls_key_path']};", block)

	def test_nothing_but_a_redirect_stays_in_the_clear(self):
		plaintext = server_with(self.config, "listen 80 default_server;")
		self.assertIn("return 301 https://$host$request_uri;", plaintext)
		self.assertNotIn("location /grove-admin/", plaintext)

	def test_the_box_name_is_the_default_server(self):
		# The Monitoring Agent connects to the bare IP and sends no SNI, so the block holding
		# /metrics/node has to be the one nginx falls back to.
		self.assertIn("listen 443 ssl default_server;", self.box)

	def test_no_key_material_reaches_the_rendered_config(self):
		self.assertNotIn("secret-material", self.config)
		self.assertNotIn("BEGIN CERTIFICATE", self.config)


class TestNoTenantStateReachesAnIngress(unittest.TestCase):
	"""The security payoff of the split, asserted rather than left to convention: an ingress
	compromise must leak no API keys. What the box is GIVEN is the whole enforcement — one binary
	runs both planes — so the guard belongs on the playbook that gives it."""

	def plays(self):
		for path in sorted(INGRESS.glob("*.yml")):
			[play] = yaml.safe_load(path.read_text())
			yield path.name, play

	def test_no_play_hands_an_ingress_a_tenant_variable(self):
		for name, play in self.plays():
			with self.subTest(name):
				rendered = str(play)
				for tenant in ("GROVE_GATEWAY_ID", "synthetic_session_ttl", "api_key", "grove_keys"):
					self.assertNotIn(tenant, rendered)

	def test_the_agent_is_told_it_is_an_ingress(self):
		# GROVE_INGRESS_ID is the mode switch. A box given neither id, or both, is not a shape the
		# agent should ever see.
		envs = [str(play) for name, play in self.plays() if "agent.env" in str(play)]
		self.assertTrue(envs)
		for env in envs:
			self.assertIn("GROVE_INGRESS_ID", env)


class TestPlaysAgreeOnTheCertificate(unittest.TestCase):
	def plays(self):
		for name in ("ingress.yml", "deploy_openresty.yml"):
			[play] = yaml.safe_load((INGRESS / name).read_text())
			yield name, play

	def test_every_play_that_writes_the_config_also_installs_the_certificate(self):
		# nginx.conf names fleet.crt whenever a zone is set, and OpenResty refuses to start
		# without the file. A play that renders one but not the other takes the box down.
		for name, play in self.plays():
			with self.subTest(name):
				roles = [r if isinstance(r, str) else r.get("role") for r in play.get("roles") or []]
				self.assertIn("fleet_tls", roles)

	def test_every_play_defines_the_handler_the_role_notifies(self):
		# fleet_tls deliberately declares no handler of its own, so a play that forgets one fails
		# at the first change instead of shadowing someone else's.
		for name, play in self.plays():
			with self.subTest(name):
				handlers = [handler["name"] for handler in play.get("handlers") or []]
				self.assertIn("reload openresty", handlers)

	def test_the_config_is_validated_before_anything_reloads(self):
		for name, play in self.plays():
			with self.subTest(name):
				self.assertTrue(any("openresty -t" in str(task) for task in play.get("tasks") or []))


if __name__ == "__main__":
	unittest.main()
