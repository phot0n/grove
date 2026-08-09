# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The gateway's own nginx config, in both shapes it renders. Pure — renders the template, no
site and no box.

The whole point of the file is which server block a location hangs under: get it wrong and either
customer traffic answers on a name no certificate covers, or the admin API and the scrape stay in
the clear. nginx will not tell you — every wrong arrangement parses.
"""

import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

PLAYBOOKS = Path(__file__).parent.parent.parent / "playbooks"
PROXY = PLAYBOOKS / "gateway_server"

ZONE = "grove.example.com"
GATEWAY_HOST = f"api.{ZONE}"
PROXY_HOSTNAME = f"use1-p1.{ZONE}"


def environment(templates_dir):
	# Ansible renders with trim_blocks on; matching it is the whole point of this file.
	return Environment(
		loader=FileSystemLoader(templates_dir), trim_blocks=True, keep_trailing_newline=True
	)


def role_defaults(path):
	return yaml.safe_load((path / "defaults/main.yml").read_text()) or {}


def resolve(variables):
	"""A default that references another default (fleet_tls_cert_path → fleet_tls_dir) is resolved
	lazily by Ansible and not at all by plain Jinja. Without this the config renders with the
	braces still in it and every path assertion passes against a string nginx could not use."""
	plain = Environment()
	for _ in range(2):
		variables = {
			key: plain.from_string(value).render(**variables)
			if isinstance(value, str) and "{{" in value
			else value
			for key, value in variables.items()
		}
	return variables


TEMPLATES = environment(PROXY)

# What a proxy box has in scope: the roles gateway.yml lists, whose defaults become play vars.
BASE = resolve({
	**role_defaults(PLAYBOOKS / "roles/openresty"),
	**role_defaults(PLAYBOOKS / "roles/grove_https"),
	**role_defaults(PLAYBOOKS / "roles/fleet_tls"),
})

TLS = {
	**BASE,
	"gateway_host": GATEWAY_HOST,
	"proxy_hostname": PROXY_HOSTNAME,
	"fleet_tls_cert": "-----BEGIN CERTIFICATE-----\nnot-a-real-one\n-----END CERTIFICATE-----\n",
	"fleet_tls_key": "-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----\n",
}


def render(variables):
	return TEMPLATES.get_template("nginx.conf.j2").render(**variables)


def servers(config):
	"""The config split into one string per `server {` block. Crude, and enough: the file has no
	nested braces at server level, and every assertion here is "which block is this location in",
	which substring checks on the whole file cannot answer at all."""
	blocks = config.split("\n\tserver {")
	return blocks[1:]


def server_with(config, listen_or_name):
	[block] = [b for b in servers(config) if listen_or_name in b]
	return block


class TestPlaintextShape(unittest.TestCase):
	"""No Fleet Zone: what every proxy served before there was a certificate. Asserted because it
	is the escape hatch — a fleet mid-migration renders this, and it has to keep working."""

	def setUp(self):
		self.config = render(BASE)

	def test_it_is_a_whole_config_not_a_conf_d_snippet(self):
		for directive in ("events {", "http {", "worker_processes"):
			with self.subTest(directive):
				self.assertIn(directive, self.config)

	def test_everything_still_answers_on_port_eighty(self):
		plaintext = server_with(self.config, "listen 80 default_server;")
		for location in ("location /v1/", "location = /v1/models", "location /grove-admin/"):
			with self.subTest(location):
				self.assertIn(location, plaintext)

	def test_metrics_keep_their_own_tls_listener(self):
		metrics = server_with(self.config, "location = /metrics/node")
		self.assertIn("listen 443 ssl default_server;", metrics)
		self.assertIn("auth_basic_user_file", metrics)

	def test_it_falls_back_to_the_self_signed_box_certificate(self):
		# fleet.crt is not on a box with no zone, and naming it would stop OpenResty starting.
		self.assertIn(BASE["grove_tls_cert"], self.config)
		self.assertNotIn(BASE["fleet_tls_cert_path"], self.config)


class TestTlsShape(unittest.TestCase):
	"""With a Fleet Zone: two names, one wildcard, and nothing but a redirect in the clear."""

	def setUp(self):
		self.config = render(TLS)
		self.plaintext = server_with(self.config, "listen 80 default_server;")
		self.customer = server_with(self.config, f"server_name {GATEWAY_HOST};")
		self.box = server_with(self.config, f"server_name {PROXY_HOSTNAME};")

	def test_nothing_but_a_redirect_is_left_in_the_clear(self):
		self.assertIn("return 301 https://$host$request_uri;", self.plaintext)
		for location in ("location /v1/", "location = /v1/models", "location /grove-admin/"):
			with self.subTest(location):
				self.assertNotIn(location, self.plaintext)

	def test_customer_traffic_answers_on_the_geodns_name(self):
		# Every proxy in the fleet answers to this one, which is why the certificate is a
		# wildcard issued centrally rather than one per box.
		self.assertIn("listen 443 ssl;", self.customer)
		self.assertIn("location /v1/", self.customer)
		self.assertIn("location = /v1/models", self.customer)

	def test_the_data_path_keeps_every_lua_phase(self):
		# Auth, metering and the request id all live in these. A location moved between servers
		# without them parses, serves, and silently bills nobody.
		for directive in (
			"access_by_lua_file /etc/grove-gateway/lua/access.lua;",
			"body_filter_by_lua_file /etc/grove-gateway/lua/body_filter.lua;",
			"log_by_lua_file /etc/grove-gateway/lua/log.lua;",
			"proxy_buffering off;",
			"proxy_read_timeout 600s;",
		):
			with self.subTest(directive):
				self.assertIn(directive, self.customer)

	def test_the_client_address_is_stated_by_the_edge_not_by_the_caller(self):
		# nginx forwards unknown request headers untouched, so without these a caller's own
		# X-Forwarded-For reaches the engine verbatim. $proxy_add_x_forwarded_for would append
		# to that claim instead of replacing it, which is the bug this asserts against.
		self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", self.customer)
		self.assertIn("proxy_set_header X-Real-IP $remote_addr;", self.customer)
		self.assertNotIn("$proxy_add_x_forwarded_for", self.customer)

	def test_a_hostname_backend_can_be_reached_at_all(self):
		# $upstream is a variable, so nginx resolves per request. Without a resolver a hostname
		# engine_url (a provider's TLS proxy) fails outright, and without SNI its handshake
		# does — while every IP backend keeps working, so nothing here fails loudly.
		for directive in ("resolver 127.0.0.53", "proxy_ssl_server_name on;"):
			with self.subTest(directive):
				self.assertIn(directive, self.customer)

	def test_the_admin_api_answers_only_on_this_box_name(self):
		# It has to reach ONE proxy; Gateway Host deliberately names them all.
		self.assertIn("location /grove-admin/", self.box)
		self.assertNotIn("location /grove-admin/", self.customer)

	def test_the_scrape_still_lands_without_sni(self):
		# The Monitoring Agent connects to the bare IP, so it sends no server name and gets
		# whichever block is default_server. Anywhere else and every proxy target goes down.
		self.assertIn("listen 443 ssl default_server;", self.box)
		self.assertIn("location = /metrics/node", self.box)
		self.assertIn("auth_basic_user_file", self.box)

	def test_exactly_one_default_server_per_port(self):
		self.assertEqual(self.config.count("listen 443 ssl default_server;"), 1)
		self.assertEqual(self.config.count("listen 80 default_server;"), 1)

	def test_both_names_present_the_same_wildcard(self):
		for block in (self.customer, self.box):
			with self.subTest(block=block.splitlines()[1].strip()):
				self.assertIn(f"ssl_certificate     {TLS['fleet_tls_cert_path']};", block)
				self.assertIn(f"ssl_certificate_key {TLS['fleet_tls_key_path']};", block)

	def test_no_key_material_reaches_the_rendered_config(self):
		# The certificate and key arrive as extra-vars for the fleet_tls role. This file names
		# their paths and must never inline either.
		self.assertNotIn("secret-material", self.config)
		self.assertNotIn("BEGIN CERTIFICATE", self.config)


class TestPlaysAgreeOnTheCertificate(unittest.TestCase):
	def plays(self):
		for name in ("gateway.yml", "deploy_openresty.yml", "deploy_tls.yml"):
			[play] = yaml.safe_load((PROXY / name).read_text())
			yield name, play

	def test_every_play_that_writes_the_config_also_installs_the_certificate(self):
		# nginx.conf names fleet.crt whenever a zone is set, and OpenResty refuses to start
		# without the file. A play that renders one but not the other takes the box down.
		for name, play in self.plays():
			with self.subTest(name):
				roles = [r if isinstance(r, str) else r.get("role") for r in play.get("roles") or []]
				self.assertIn("fleet_tls", roles)

	def test_the_certificate_lands_before_the_config_is_validated(self):
		# Roles run before tasks — that ordering is what makes this true, and it is why
		# fleet_tls is a role rather than a task block in either play.
		for name, play in self.plays():
			with self.subTest(name):
				self.assertTrue(any("openresty -t" in str(task) for task in play.get("tasks") or []))

	def test_every_play_defines_the_handler_the_role_notifies(self):
		# fleet_tls deliberately declares no handler of its own, so a play that forgets one
		# fails at the first change instead of shadowing someone else's.
		for name, play in self.plays():
			with self.subTest(name):
				handlers = [h.get("name") for h in play.get("handlers") or []]
				self.assertIn("reload openresty", handlers)

	def test_the_private_key_task_is_not_logged(self):
		# A task RESULT echoes module args back, and Grove stores that JSON on an Ansible Task
		# doc — without no_log the fleet key is readable in the Desk UI.
		[block] = yaml.safe_load((PLAYBOOKS / "roles/fleet_tls/tasks/main.yml").read_text())
		key_task = [t for t in block["block"] if "key" in t["name"]]
		self.assertEqual(len(key_task), 1)
		self.assertTrue(key_task[0]["no_log"])


if __name__ == "__main__":
	unittest.main()
