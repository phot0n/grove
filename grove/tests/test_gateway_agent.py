# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What the Deploy Agent button ships. Pure — the doc is a SimpleNamespace and run_playbook is
recorded, so no site and no SSH.

The play writes /etc/grove-gateway/agent.env whole, from extra-vars. That file holds every listener,
name, path and secret the gateway has, so a caller that omits one variable does not leave a stale
value behind — it writes a BLANK one. Blank is fatal for the admin token and silently wrong for a
hostname, which is why these pin that the button passes everything the file renders.

The tunables are the other half, and they go to config.json instead. Both are asserted here because
one button ships both.
"""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import yaml

from grove.fleet import GATEWAY_AGENT_VERSION, FleetHost
from grove.grove.doctype.gateway_server.gateway_server import GatewayServer
from grove.grove.doctype.ingress_server.ingress_server import IngressServer

# Anchored to this file, not the cwd: `bench run-tests` runs from the bench root, where a path
# relative to the app does not resolve.
PLAYBOOKS = Path(__file__).parent.parent / "playbooks"
TTL = "2m"

SETTINGS = SimpleNamespace(
	gateway_variables={"synthetic_session_ttl": TTL},
	scrape_auth_variables={"scrape_password_hash": "$2b$12$hash"},
	tls_variables={
		"gateway_host": "api.grove.test",
		"fleet_zone": "grove.test",
		"fleet_tls_cert": "-----BEGIN CERTIFICATE-----",
		"fleet_tls_key": "-----BEGIN PRIVATE KEY-----",
	},
)


def extravars_for(doctype_class, module, doc):
	"""Run _deploy_agent against a fake doc and return the extra-vars it passed."""
	sent = {}

	def run_playbook(play, extravars):
		sent.update({"play": play, **extravars})
		return "play-1", 0

	doc.run_playbook = run_playbook
	# Recording is FleetHost's, and TestTheInstalledVersionIsRecorded holds it to account.
	doc.record_agent_version = lambda rc: None
	with patch("frappe.get_single", return_value=SETTINGS):
		doctype_class._deploy_agent(doc)
	return sent


def gateway_extravars():
	return extravars_for(
		GatewayServer,
		"grove.grove.doctype.gateway_server.gateway_server",
		SimpleNamespace(
			name="gw-1",
			region="ap-south-1",
			hostname="gw-1.grove.test",
			get_password=lambda field, **kwargs: f"secret-{field}",
		),
	)


def ingress_extravars():
	return extravars_for(
		IngressServer,
		"grove.grove.doctype.ingress_server.ingress_server",
		SimpleNamespace(
			name="ing-1",
			hostname="ing-1.grove.test",
			get_password=lambda field, **kwargs: f"secret-{field}",
		),
	)


def rendered_variables(play_path, task_name, field):
	"""The Jinja variables a task's content renders, read from the play itself."""
	tasks = yaml.safe_load(Path(play_path).read_text())[0]["tasks"]
	content = next(t["ansible.builtin.copy"][field] for t in tasks if t["name"] == task_name)
	return set(re.findall(r"{{\s*(\w+)", content))


def upgrade_handler(plane):
	handlers = yaml.safe_load((PLAYBOOKS / plane / "deploy_agent.yml").read_text())[0]["handlers"]
	return next(handler for handler in handlers if handler["name"] == "upgrade grove-gateway")


def install_role_tasks():
	path = PLAYBOOKS / "roles" / "install_gateway_agent" / "tasks" / "main.yml"
	return yaml.safe_load(path.read_text())


def agent_env_variables(plane):
	return rendered_variables(PLAYBOOKS / plane / "deploy_agent.yml", "agent.env", "content")


def config_json_variables(plane):
	template = (PLAYBOOKS / plane / "config.json.j2").read_text()
	return set(re.findall(r"{{\s*(\w+)", template))


# Variables the ROLES supply, not the button. agent.env names the certificate and htpasswd paths,
# which are role defaults — deploy_agent includes grove_https and fleet_tls for exactly that.
FROM_ROLES = {
	"fleet_tls_cert_path",
	"fleet_tls_key_path",
	"grove_tls_cert",
	"grove_tls_key",
	"grove_metrics_htpasswd",
}


class TestDeployAgentShipsBothHalves(unittest.TestCase):
	def test_it_runs_the_agent_play(self):
		self.assertEqual(gateway_extravars()["play"], "deploy_agent.yml")
		self.assertEqual(ingress_extravars()["play"], "deploy_agent.yml")

	def test_it_passes_every_variable_the_env_file_renders(self):
		"""The general form of the hazard: the next variable added to agent.env is written blank
		unless the button passes it too."""
		for plane, sent in (("gateway_server", gateway_extravars()), ("ingress_server", ingress_extravars())):
			with self.subTest(plane):
				missing = agent_env_variables(plane) - set(sent) - FROM_ROLES
				self.assertEqual(set(), missing)

	def test_it_passes_every_variable_the_tunables_file_renders(self):
		# A tunable the button forgets renders as its Jinja default, which is not the same as the
		# value in Grove Settings — the box would quietly run something nobody chose.
		for plane, sent in (("gateway_server", gateway_extravars()), ("ingress_server", ingress_extravars())):
			with self.subTest(plane):
				# `default(...)` filters make a variable optional; only the bare ones are required.
				required = {
					name for name in config_json_variables(plane)
					if not re.search(rf"{{{{\s*{name}\s*\|\s*default", (PLAYBOOKS / plane / "config.json.j2").read_text())
				}
				self.assertEqual(set(), required - set(sent))

	def test_a_new_binary_reaches_a_box_whose_gateway_is_stopped(self):
		"""The systemd module's `state: reloaded` refuses on a unit that is not active, and a box with
		a stopped gateway is the likeliest one to be having a binary shipped to it — the deploy failed
		on the handler with the fix already sitting on disk, unstarted."""
		for plane in ("gateway_server", "ingress_server"):
			with self.subTest(plane):
				handler = upgrade_handler(plane)
				self.assertNotIn("ansible.builtin.systemd", handler)
				self.assertIn("reload-or-restart", handler["ansible.builtin.command"])

	def test_the_admin_token_is_not_blank(self):
		"""A blank one is fatal (the process refuses to start), so the box would come back
		crash-looping instead of serving."""
		self.assertTrue(gateway_extravars()["admin_token"])
		self.assertTrue(ingress_extravars()["admin_token"])

	def test_an_ingress_is_given_its_data_token(self):
		# Blank, an ingress refuses every gateway — which reads as a routing outage rather than the
		# config fault it is.
		self.assertTrue(ingress_extravars()["data_token"])

	def test_each_box_is_told_its_own_name(self):
		# agent.env carries the name the admin plane and the scrape answer on. Blank means the
		# control plane can still reach the box, but only by the shared name — which reaches all of
		# them.
		self.assertEqual("gw-1.grove.test", gateway_extravars()["proxy_hostname"])
		self.assertEqual("ing-1.grove.test", ingress_extravars()["ingress_hostname"])

	def test_the_tuning_comes_from_grove_settings(self):
		self.assertEqual(TTL, gateway_extravars()["synthetic_session_ttl"])

	def test_it_asks_for_the_release_the_control_plane_is_pinned_to(self):
		# The agent lives in its own repo, so this variable is the only thing that decides which
		# binary a box ends up running.
		self.assertEqual(GATEWAY_AGENT_VERSION, gateway_extravars()["agent_version"])
		self.assertEqual(GATEWAY_AGENT_VERSION, ingress_extravars()["agent_version"])

	def test_the_fleet_private_key_never_rides_along(self):
		# A config push has no business carrying it: agent.env names the certificate's PATH, and
		# deploy_tls owns writing the material. Resolved in _deploy_agent rather than at enqueue so
		# it never reaches the job payload in Redis either.
		for plane, sent in (("gateway", gateway_extravars()), ("ingress", ingress_extravars())):
			with self.subTest(plane):
				self.assertNotIn("fleet_tls_key", sent)
				self.assertIn("fleet_tls_cert", sent)


class TestTheBinaryComesOffTheInternetSafely(unittest.TestCase):
	"""The agent is built in its own repo now, so a box downloads a release instead of compiling a
	source tree the control plane handed it."""

	def test_the_download_is_checksummed(self):
		# Without this the play installs whatever answers the URL, which is a worse failure than
		# the one it replaced: the old source tree at least came from the control plane's own disk.
		download = next(task for task in install_role_tasks() if "ansible.builtin.get_url" in task)
		self.assertTrue(download["ansible.builtin.get_url"]["checksum"].startswith("sha256:"))

	def test_the_release_it_reaches_for_is_the_pinned_one(self):
		defaults = PLAYBOOKS / "roles" / "install_gateway_agent" / "defaults" / "main.yml"
		self.assertIn("agent_version", yaml.safe_load(defaults.read_text())["agent_release_url"])

	def test_nothing_compiles_on_the_target_any_more(self):
		# A box that still installs Go is a box that needs a toolchain and outbound access to the
		# module proxy — the two things this split was meant to take off the fleet.
		self.assertNotIn("go build", yaml.dump(install_role_tasks()))


class TestTheInstalledVersionIsRecorded(unittest.TestCase):
	"""One repo made version skew impossible. Two makes it the thing to watch, and the doc is where
	it shows."""

	def set_value_after(self, rc):
		# frappe.db is a Local and unbound without a site, so the whole thing is swapped — the
		# same way test_agent_sync reaches it.
		doc = SimpleNamespace(doctype="Gateway Server", name="gw-1")
		db = Mock()
		with patch.object(frappe, "db", db):
			FleetHost.record_agent_version(doc, rc)
		return db.set_value

	def test_a_finished_play_records_what_it_installed(self):
		self.set_value_after(0).assert_called_once_with(
			"Gateway Server", "gw-1", "agent_version", GATEWAY_AGENT_VERSION
		)

	def test_a_failed_play_leaves_the_old_version_standing(self):
		# The box is still running whatever it was running. Claiming the new one would hide exactly
		# the skew this field exists to show.
		self.set_value_after(1).assert_not_called()
