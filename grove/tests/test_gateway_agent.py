# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""What the Deploy Agent button ships. Pure — the doc is a SimpleNamespace and run_playbook is
recorded, so no site and no SSH.

The play writes /etc/pathway/agent.env whole, from extra-vars. That file holds every listener,
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

from grove.fleet import FleetHost, gateway_agent_release, gateway_agent_version
from grove.grove.doctype.gateway_server.gateway_server import GatewayServer
from grove.grove.doctype.ingress_server.ingress_server import IngressServer

# Anchored to this file: `bench run-tests` runs from the bench root.
PLAYBOOKS = Path(__file__).parent.parent / "playbooks"
TTL = "2m"
# Deliberately not a real release: these fail if the version goes back to being hardcoded.
PINNED = "v9.9.9"

SETTINGS = SimpleNamespace(
	gateway_variables={"synthetic_session_ttl": TTL},
	scrape_auth_variables={"scrape_password_hash": "$2b$12$hash"},
)
# The Gateway Store a fake gateway runs on.
STORE = SimpleNamespace(redis_variables={"redis_addr": "10.0.61.9:6379", "redis_password": "pw"})
# What a box's Geography hands it.
TLS = {
	"fleet_zone": "grove.test",
	"fleet_tls_cert": "-----BEGIN CERTIFICATE-----",
	"fleet_tls_key": "-----BEGIN PRIVATE KEY-----",
}


def geography_endpoint(doctype, name, field):
	assert (doctype, name, field) == ("Geography", "eu", "endpoint")
	return "eu.grove.test"


def extravars_for(doctype_class, module, doc, **kwargs):
	"""Run _deploy_agent against a fake doc and return the extra-vars it passed."""
	sent = {}

	def run_playbook(play, extravars, **play_kwargs):
		sent.update({"play": play, **extravars, "play_kwargs": play_kwargs})
		return "play-1", 0

	doc.run_playbook = run_playbook
	# Recording is FleetHost's, and TestTheInstalledVersionIsRecorded holds it to account.
	doc.record_agent_version = lambda rc: None
	doc.record_store = lambda rc, store: None
	# frappe.db is a Local and unbound without a site; the release is read off Grove Settings
	# through it, so the whole thing is swapped the way test_pathway_sync reaches it.
	with (
		patch("frappe.get_single", return_value=SETTINGS),
		patch.object(
			frappe, "db", SimpleNamespace(get_single_value=lambda *args: PINNED, get_value=geography_endpoint)
		),
		# A gateway's Redis is its store's, read off the Gateway Store doc.
		patch.object(frappe, "get_doc", return_value=STORE),
	):
		doctype_class._deploy_agent(doc, **kwargs)
	return sent


class FakeGateway(SimpleNamespace):
	get_agent_extravars = GatewayServer.get_agent_extravars
	config_variables = GatewayServer.config_variables
	short_name = GatewayServer.short_name


class FakeIngress(SimpleNamespace):
	config_variables = IngressServer.config_variables
	short_name = IngressServer.short_name


def fake_ingress(**fields):
	defaults = dict(
		name="ing-1", hostname="ing-1.grove.test", is_in_maintenance=0, tls_variables=dict(TLS),
		get_password=lambda field, **kwargs: f"secret-{field}",
	)
	return FakeIngress(**{**defaults, **fields})


def fake_gateway(**fields):
	defaults = dict(
		name="gw-1",
		region="ap-south-1",
		geography="eu",
		tls_variables=dict(TLS),
		hostname="gw-1.grove.test",
		gateway_store="store1",
		network_store="store1",
		is_in_maintenance=0,
		get_password=lambda field, **kwargs: f"secret-{field}",
	)
	return FakeGateway(**{**defaults, **fields})


def gateway_extravars(**fields):
	return extravars_for(GatewayServer, "grove.grove.doctype.gateway_server.gateway_server", fake_gateway(**fields))


def ingress_extravars(**fields):
	return extravars_for(IngressServer, "grove.grove.doctype.ingress_server.ingress_server", fake_ingress(**fields))


def rendered_variables(play_path, task_name, field):
	"""The Jinja variables a task's content renders, read from the play itself."""
	tasks = yaml.safe_load(Path(play_path).read_text())[0]["tasks"]
	content = next(t["ansible.builtin.copy"][field] for t in tasks if t["name"] == task_name)
	return set(re.findall(r"{{\s*(\w+)", content))


def upgrade_handler(plane):
	handlers = yaml.safe_load((PLAYBOOKS / plane / "deploy_agent.yml").read_text())[0]["handlers"]
	return next(handler for handler in handlers if handler["name"] == "upgrade pathway")


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

	def test_a_gateway_answers_for_its_geography(self):
		# The endpoint lives on the Geography; a pinned user from elsewhere is refused by this name.
		sent = gateway_extravars()
		self.assertEqual(("eu", "eu.grove.test"), (sent["gateway_geography"], sent["gateway_host"]))
		unplaced = gateway_extravars(geography=None)
		self.assertEqual(("", ""), (unplaced["gateway_geography"], unplaced["gateway_host"]))

	def test_a_box_named_with_its_domain_is_known_to_the_agent_by_its_label(self):
		# The id is stamped into request ids, which keep only letters, digits and '-'.
		self.assertEqual("gw-2", gateway_extravars(name="gw-2.eu.grove.test")["gateway_id"])
		self.assertEqual("ing-2", ingress_extravars(name="ing-2.eu.grove.test")["ingress_id"])

	def test_the_tuning_comes_from_grove_settings(self):
		self.assertEqual(TTL, gateway_extravars()["synthetic_session_ttl"])

	def test_it_asks_for_the_release_the_fleet_is_pinned_to(self):
		# The agent lives in its own repo, so this variable is the only thing that decides which
		# binary a box ends up running — and it comes from Grove Settings, so a rollback is an edit
		# and a Deploy Agent rather than a control-plane release.
		self.assertEqual(PINNED, gateway_extravars()["agent_version"])
		self.assertEqual(PINNED, ingress_extravars()["agent_version"])

	def test_the_repo_comes_from_grove_settings_and_blank_is_refused(self):
		values = {"pathway_release": PINNED, "pathway_repo": "someone/pathway"}
		db = SimpleNamespace(get_single_value=lambda doctype, field: values[field])
		with patch.object(frappe, "db", db):
			self.assertEqual({"agent_version": PINNED, "agent_repo": "someone/pathway"}, gateway_agent_release())
			values["pathway_repo"] = None
			with patch("frappe.throw", side_effect=frappe.ValidationError), self.assertRaises(frappe.ValidationError):
				gateway_agent_release()

	def test_a_fleet_that_pins_nothing_is_refused_before_it_ships(self):
		# Blank renders an empty release tag into the download URL, which 404s the play twenty
		# minutes in. A Single doc predating the field never applies its JSON default, so this is a
		# state a real site lands in rather than a hypothetical.
		with (
			patch.object(frappe, "db", SimpleNamespace(get_single_value=lambda *args: None)),
			patch("frappe.throw", side_effect=frappe.ValidationError),
			self.assertRaises(frappe.ValidationError),
		):
			gateway_agent_version()

	def test_the_fleet_private_key_never_rides_along(self):
		# A config push has no business carrying it: agent.env names the certificate's PATH, and
		# deploy_tls owns writing the material. Resolved in _deploy_agent rather than at enqueue so
		# it never reaches the job payload in Redis either.
		for plane, sent in (("gateway", gateway_extravars()), ("ingress", ingress_extravars())):
			with self.subTest(plane):
				self.assertNotIn("fleet_tls_key", sent)
				self.assertIn("fleet_tls_cert", sent)


class TestAGatewayIsToldWhichRedis(unittest.TestCase):
	"""agent.env names the Redis whole, so a deploy that forgot the store would restart a gateway
	onto nothing. It is refused instead."""

	def test_a_deploy_onto_no_store_is_refused(self):
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError), self.assertRaises(frappe.ValidationError):
			GatewayServer.get_agent_extravars(fake_gateway(gateway_store=None), store=None)

	def test_a_deploy_never_moves_a_gateway_onto_its_networks_store(self):
		# Moving a live gateway drains it first; a routine deploy must not do that by the way.
		doc = fake_gateway(
			gateway_store="s-current", network_store="s-network",
			record_agent_version=lambda rc: None, record_store=lambda rc, store: None,
			run_playbook=lambda play, extravars: ("play-1", 0),
		)
		doc.get_agent_extravars = Mock(return_value={})
		GatewayServer._deploy_agent(doc)
		self.assertEqual("s-current", doc.get_agent_extravars.call_args.args[0])

	def provisioned_onto(self, agent_version):
		"""The store provision hands get_agent_extravars, for a gateway on s-current in a Network
		whose store is s-network."""
		doc = fake_gateway(
			doctype="Gateway Server", agent_version=agent_version, gateway_store="s-current",
			network_store="s-network", admin_url="", set_admin_url=lambda: None,
			record_agent_version=lambda rc: None, record_store=lambda rc, store: None,
			run_playbook=lambda play, extravars: ("play-1", 1),
		)
		doc.get_agent_extravars = Mock(return_value={})
		with patch.object(frappe, "db", Mock()), patch("frappe.get_single", return_value=SETTINGS):
			GatewayServer.provision(doc)
		return doc.get_agent_extravars.call_args.args[0]

	def test_setup_puts_a_gateway_on_its_networks_store(self):
		for agent_version in (None, "v1"):
			with self.subTest(agent_version):
				self.assertEqual("s-network", self.provisioned_onto(agent_version))

	def test_a_gateway_on_a_store_is_given_the_stores(self):
		sent = gateway_extravars()
		self.assertEqual(STORE.redis_variables, {key: sent[key] for key in STORE.redis_variables})

	def recorded(self, store, rc=0, before=(None, 0), writers=()):
		"""What record_store writes, given the gateway's store and flag before the play and the
		store's Active writers."""
		db = Mock()
		db.get_value.return_value = frappe._dict(gateway_store=before[0], is_store_writer=before[1])
		with (
			patch.object(frappe, "db", db),
			patch("grove.grove.doctype.gateway_server.gateway_server.store_writers", return_value=list(writers)),
		):
			GatewayServer.record_store(SimpleNamespace(doctype="Gateway Server", name="gw-1"), rc, store)
		return db.set_value.call_args.args[2] if db.set_value.called else None

	def test_nothing_is_recorded_when_the_play_failed(self):
		self.assertIsNone(self.recorded("s1", rc=1))

	def test_the_first_gateway_on_a_store_becomes_its_writer(self):
		self.assertEqual(self.recorded("s1"), {"gateway_store": "s1", "is_store_writer": 1})

	def test_a_store_that_has_a_writer_takes_this_one_as_a_reader(self):
		self.assertEqual(self.recorded("s1", writers=["gw-0"]), {"gateway_store": "s1", "is_store_writer": 0})

	def test_a_writer_redeployed_onto_its_store_stays_one(self):
		recorded = self.recorded("s1", before=("s1", 1), writers=["gw-0", "gw-1"])
		self.assertEqual(recorded["is_store_writer"], 1)

	def test_only_a_gateway_on_a_store_can_be_its_writer(self):
		doc = SimpleNamespace(name="gw-1", is_store_writer=1, gateway_store=None, set_admin_url=lambda: None, set_admin_token=lambda: None)
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError), self.assertRaises(frappe.ValidationError):
			GatewayServer.validate(doc)


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
		# same way test_pathway_sync reaches it.
		doc = SimpleNamespace(doctype="Gateway Server", name="gw-1")
		db = Mock()
		db.get_single_value.return_value = PINNED
		with patch.object(frappe, "db", db):
			FleetHost.record_agent_version(doc, rc)
		return db.set_value

	def test_a_finished_play_records_what_it_installed(self):
		self.set_value_after(0).assert_called_once_with(
			"Gateway Server", "gw-1", "agent_version", PINNED
		)

	def test_a_failed_play_leaves_the_old_version_standing(self):
		# The box is still running whatever it was running. Claiming the new one would hide exactly
		# the skew this field exists to show.
		self.set_value_after(1).assert_not_called()


class TestPingReachesHealthz(unittest.TestCase):
	"""Ping tells unreachable (throws) apart from reachable-but-unhealthy (a 503 with its reason)."""

	def ping(self, **get):
		doc = SimpleNamespace(doctype="Gateway Server", name="gw-1", admin_url="https://gw-1.fleet.test/grove-admin")
		doc.health_url = FleetHost.health_url.fget(doc)
		with (
			patch("grove.fleet.requests.get", **get) as request,
			patch("frappe.msgprint") as msgprint,
			patch("frappe.throw", side_effect=frappe.ValidationError),
		):
			result = FleetHost.ping(doc)
		request.assert_called_once_with("https://gw-1.fleet.test/healthz", timeout=5)
		return result, msgprint

	def test_an_unhealthy_box_is_still_reachable(self):
		response = Mock(status_code=503, ok=False, text="maintenance\n")
		response.elapsed.total_seconds.return_value = 0.042
		result, msgprint = self.ping(return_value=response)
		self.assertEqual(503, result)
		self.assertIn("503 in 42 ms: maintenance", msgprint.call_args.args[0])
		self.assertEqual("orange", msgprint.call_args.kwargs["indicator"])

	def test_an_unreachable_box_raises(self):
		import requests

		with self.assertRaises(requests.ConnectionError):
			self.ping(side_effect=requests.ConnectionError("refused"))


class TestALocalBuildCanStandInForTheRelease(unittest.TestCase):
	"""A dev deploy. The binary comes off the control plane, nothing is downloaded, and the rest of
	the provision — unit, user, config — runs exactly as it does for a release."""

	def test_naming_a_build_skips_the_download(self):
		for task in install_role_tasks():
			if "ansible.builtin.get_url" in task or task.get("name", "").startswith("stage the binary under"):
				with self.subTest(task["name"]):
					self.assertIn("agent_binary", str(task["when"]))

	def test_the_build_lands_where_the_install_task_copies_from(self):
		stage = next(t for t in install_role_tasks() if t.get("name") == "stage a local build instead")
		self.assertEqual("/tmp/pathway", stage["ansible.builtin.copy"]["dest"])
		self.assertEqual("{{ agent_binary }}", stage["ansible.builtin.copy"]["src"])
		self.assertNotIn("remote_src", stage["ansible.builtin.copy"])

	def test_the_default_is_the_release(self):
		defaults = PLAYBOOKS / "roles" / "install_gateway_agent" / "defaults" / "main.yml"
		self.assertEqual("", yaml.safe_load(defaults.read_text())["agent_binary"])


class TestADevDeployShipsALocalBuild(unittest.TestCase):
	"""The button ships the pinned release. A job started with `agent_binary` ships a build off the
	control plane instead — the same play, the same tracking, nothing copied by hand."""

	def test_the_button_ships_the_release(self):
		self.assertEqual("", gateway_extravars()["agent_binary"])

	def test_a_named_build_reaches_the_play(self):
		sent = extravars_for(
			GatewayServer,
			"grove.grove.doctype.gateway_server.gateway_server",
			fake_gateway(),
			agent_binary="/builds/pathway",
		)
		self.assertEqual("/builds/pathway", sent["agent_binary"])


class TestAnUpdateOwnsItsPlays(unittest.TestCase):
	def test_both_planes_hand_the_reference_to_the_play(self):
		reference = {"reference_doctype": "Pathway Update", "reference_docname": "r1"}
		gateway = extravars_for(
			GatewayServer, "grove.grove.doctype.gateway_server.gateway_server", fake_gateway(), **reference
		)
		ingress = extravars_for(
			IngressServer, "grove.grove.doctype.ingress_server.ingress_server", fake_ingress(), **reference
		)
		self.assertEqual(reference, gateway["play_kwargs"])
		self.assertEqual(reference, ingress["play_kwargs"])


class TestMaintenanceIsHeldInConfigJson(unittest.TestCase):
	"""Maintenance is a config.json key: every play that writes the file carries the doc's value, so
	a deploy never flips it, and the box is read back because a rejected reload is silent."""

	def test_every_deploy_carries_the_maintenance_flag(self):
		for extravars in (gateway_extravars, ingress_extravars):
			with self.subTest(extravars.__name__):
				self.assertIs(False, extravars()["gateway_maintenance"])
				self.assertIs(True, extravars(is_in_maintenance=1)["gateway_maintenance"])

	def test_an_ingress_setup_carries_it_too(self):
		doc = fake_ingress(is_in_maintenance=1)
		with (
			patch("frappe.get_single", return_value=SETTINGS),
			patch.object(frappe, "db", SimpleNamespace(get_single_value=lambda *args: PINNED)),
		):
			variables = IngressServer.provision_variables(doc, SETTINGS)
		self.assertIs(True, variables["gateway_maintenance"])

	def applied(self, box_says, rc=0):
		doc = fake_gateway(is_in_maintenance=1, get_in_flight=lambda: {"maintenance": box_says, "in_flight": 0})
		doc.run_playbook = Mock(return_value=("play-1", rc))
		with (
			patch("frappe.get_single", return_value=SETTINGS),
			patch("frappe.throw", side_effect=frappe.ValidationError),
			patch("grove.fleet.time.sleep"),
			patch("grove.failure.report"),
		):
			result = GatewayServer.apply_config(doc)
		return doc.run_playbook, result

	def test_it_writes_config_json_and_returns_once_the_box_agrees(self):
		run_playbook, result = self.applied(box_says=True)
		self.assertEqual(("play-1", 0), result)
		play, = run_playbook.call_args.args
		self.assertEqual("config.yml", play)
		self.assertIs(True, run_playbook.call_args.kwargs["extravars"]["gateway_maintenance"])

	def test_a_box_that_did_not_take_it_is_an_error(self):
		with self.assertRaises(frappe.ValidationError):
			self.applied(box_says=False)

	def test_a_failed_play_is_an_error(self):
		with self.assertRaises(frappe.ValidationError):
			self.applied(box_says=True, rc=2)

	def test_a_box_that_cannot_answer_is_never_put_in_maintenance(self):
		# A pathway older than the key refuses the whole file, even at its next start.
		import requests

		doc = fake_gateway(get_in_flight=Mock(side_effect=requests.HTTPError("404")), db_set=Mock())
		with self.assertRaises(requests.HTTPError):
			GatewayServer.set_maintenance(doc, 1)
		doc.db_set.assert_not_called()
