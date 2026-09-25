"""What the gateway plays put on a box.

Replaces the nginx-template tests. Those asserted which *server block* a location landed under,
because nginx would not tell you — every wrong arrangement parses. There is no nginx now, so what is
left to get wrong is the configuration itself, and it splits in two:

  agent.env    identity, secrets, sockets, paths — a restart to change
  config.json  tunables — re-read on SIGUSR1

The rule worth a test is that those two stay DISJOINT. A value in both is the bug this split exists
to prevent, and nothing in the binary would report it — whichever half lost would simply be ignored.
"""

import json
import pathlib
import re
import unittest

import yaml

PLAYBOOKS = pathlib.Path(__file__).resolve().parent.parent / "playbooks"

# Every environment variable the binary reads. One a play writes that is not here is a typo the
# process ignores in silence.
KNOWN_ENV = {
	"GROVE_ADMIN_TOKEN",
	"GROVE_GATEWAY_ID",
	"GROVE_INGRESS_ID",
	"GROVE_INGRESS_TOKEN",
	"GROVE_GATEWAY_REGION",
	"GROVE_GATEWAY_GEOGRAPHY",
	"GROVE_REDIS_ADDR",
	"GROVE_REDIS_PASSWORD",
	"GROVE_LISTEN_HTTP",
	"GROVE_LISTEN_HTTPS",
	"GROVE_PUBLIC_HOST",
	"GROVE_SELF_HOST",
	"GROVE_TLS_CERT",
	"GROVE_TLS_KEY",
	"GROVE_HTPASSWD",
	"GROVE_NODE_EXPORTER_URL",
	"GROVE_ACCESS_LOG",
	"GROVE_ERROR_LOG",
	"GROVE_PAYLOAD_LOG",
	"GROVE_CONFIG",
	"GROVE_PID_FILE",
}

# Every key config.json may carry. The gateway REFUSES a file with an unknown key.
KNOWN_TUNABLES = {
	"log_level",
	"middleware",
	"transforms",
	"synthetic_session_ttl",
	"max_body_bytes",
	"upstream_read_timeout",
	"upstream_tls_verify",
	"drain_timeout",
	"lame_duck",
	"upgrade_timeout",
	"maintenance",
}

PLAYS = ("gateway_server/gateway.yml", "gateway_server/deploy_agent.yml",
         "ingress_server/ingress.yml", "ingress_server/deploy_agent.yml")


def play(name):
	return yaml.safe_load((PLAYBOOKS / name).read_text())[0]


def tasks(name):
	return play(name).get("tasks", [])


def agent_env(name):
	"""The env-file content a play writes, as a dict of variable → raw Jinja."""
	for task in tasks(name):
		if task.get("name") == "agent.env":
			body = task["ansible.builtin.copy"]["content"]
			return dict(
				line.split("=", 1) for line in body.strip().splitlines() if "=" in line
			)
	raise AssertionError(f"{name} writes no agent.env")


def tunable_keys():
	"""The keys config.json.j2 renders. Read as text, not YAML: it is a Jinja template."""
	template = (PLAYBOOKS / "gateway_server/config.json.j2").read_text()
	return set(re.findall(r'^\s*"([a-z_]+)":', template, re.MULTILINE))


class TestTheTwoHalvesStayDisjoint(unittest.TestCase):
	def test_no_setting_is_in_both_halves(self):
		# The reason for the split: a value in both means one is silently ignored, and which one
		# depends on an implementation detail.
		tunables = tunable_keys()
		for name in PLAYS:
			with self.subTest(name):
				written = {key.removeprefix("GROVE_").lower() for key in agent_env(name)}
				self.assertEqual(set(), written & tunables)

	def test_every_env_variable_is_one_the_binary_reads(self):
		for name in PLAYS:
			with self.subTest(name):
				self.assertLessEqual(set(agent_env(name)), KNOWN_ENV)

	def test_every_tunable_is_one_the_binary_accepts(self):
		# The gateway refuses the file whole, so a typo leaves a box on its previous config.
		self.assertLessEqual(tunable_keys(), KNOWN_TUNABLES)

	def test_both_planes_render_the_same_tunables(self):
		gateway = (PLAYBOOKS / "gateway_server/config.json.j2").read_text()
		ingress = (PLAYBOOKS / "ingress_server/config.json.j2").read_text()
		self.assertEqual(gateway, ingress)


class TestAgentEnvIsWrittenWhole(unittest.TestCase):
	"""Every play writes the file complete, never a fragment.

	A partial write is the failure that matters: agent.env is `copy`, not `lineinfile`, so a play
	that omitted a variable would silently drop it — and a gateway missing GROVE_SELF_HOST serves
	its admin plane on no name at all.
	"""

	REQUIRED = {
		"GROVE_ADMIN_TOKEN", "GROVE_REDIS_ADDR", "GROVE_LISTEN_HTTP", "GROVE_LISTEN_HTTPS",
		"GROVE_TLS_CERT", "GROVE_TLS_KEY", "GROVE_CONFIG", "GROVE_ACCESS_LOG",
		"GROVE_ERROR_LOG",
	}

	def test_every_play_writes_the_whole_file(self):
		for name in PLAYS:
			with self.subTest(name):
				self.assertLessEqual(self.REQUIRED, set(agent_env(name)))

	def test_a_gateway_is_given_a_gateway_id_and_an_ingress_an_ingress_id(self):
		# What keeps an ingress out of the tenant plane is what it is GIVEN, and both ids set is a
		# startup refusal.
		for name, expected, forbidden in (
			("gateway_server/gateway.yml", "GROVE_GATEWAY_ID", "GROVE_INGRESS_ID"),
			("gateway_server/deploy_agent.yml", "GROVE_GATEWAY_ID", "GROVE_INGRESS_ID"),
			("ingress_server/ingress.yml", "GROVE_INGRESS_ID", "GROVE_GATEWAY_ID"),
			("ingress_server/deploy_agent.yml", "GROVE_INGRESS_ID", "GROVE_GATEWAY_ID"),
		):
			with self.subTest(name):
				written = agent_env(name)
				self.assertIn(expected, written)
				self.assertNotIn(forbidden, written)

	def test_an_ingress_is_never_handed_a_tenant_setting(self):
		# An ingress holds no keys, users or usage — a synthetic session would mean it had a view
		# of who was calling.
		for name in ("ingress_server/ingress.yml", "ingress_server/deploy_agent.yml"):
			with self.subTest(name):
				self.assertNotIn("GROVE_GATEWAY_REGION", agent_env(name))
				self.assertNotIn("GROVE_GATEWAY_GEOGRAPHY", agent_env(name))

	def test_provision_and_deploy_write_the_same_file(self):
		# Two plays write agent.env, and a key in one but not the other is a setting that silently
		# turns off on whichever runs next. Provisioning gw1 did exactly that to payload logging.
		for provision, deploy in (
			("gateway_server/gateway.yml", "gateway_server/deploy_agent.yml"),
			("ingress_server/ingress.yml", "ingress_server/deploy_agent.yml"),
		):
			with self.subTest(provision):
				self.assertEqual(set(agent_env(provision)), set(agent_env(deploy)))

	def test_the_env_file_is_never_world_readable(self):
		# It carries the admin token and, on an ingress, the data token.
		for name in PLAYS:
			with self.subTest(name):
				task = next(t for t in tasks(name) if t.get("name") == "agent.env")
				self.assertEqual("0600", task["ansible.builtin.copy"]["mode"])
				self.assertTrue(task.get("no_log"))


class TestDeployingIsNotATrafficEvent(unittest.TestCase):
	def test_a_new_binary_reloads_rather_than_restarts(self):
		"""SIGHUP hands the listening sockets to a child that serves immediately; a restart would cut
		every stream in flight, and a stream here runs for minutes.

		reload-or-restart rather than the systemd module's `state: reloaded`, which fails outright on
		a unit that is not active — and a box whose gateway is down is exactly the one someone is
		shipping a new binary to. That refusal turned a fixable box into a deploy that could not run.
		"""
		for name in ("gateway_server/deploy_agent.yml", "ingress_server/deploy_agent.yml"):
			with self.subTest(name):
				binary = next(t for t in tasks(name) if t.get("name") == "install pathway binary")
				self.assertEqual("upgrade pathway", binary["notify"])
				handler = next(h for h in play(name)["handlers"] if h["name"] == "upgrade pathway")
				self.assertIn("reload-or-restart", handler["ansible.builtin.command"])

	def test_a_changed_tunable_signals_rather_than_restarts(self):
		for name in PLAYS:
			with self.subTest(name):
				config = next(t for t in tasks(name) if t.get("name") == "config.json")
				self.assertEqual("reload pathway config", config["notify"])

	def test_agent_env_restarts_because_a_reload_would_not_see_it(self):
		# The child reads its environment from systemd, not the parent, so a reload leaves the old
		# values in place. The one case that drains.
		for name in PLAYS:
			with self.subTest(name):
				task = next(t for t in tasks(name) if t.get("name") == "agent.env")
				self.assertEqual("restart pathway", task["notify"])

	def test_a_renewed_certificate_needs_nothing(self):
		# The gateway watches the file's mtime, and deploy_tls runs unattended at midnight against
		# every Active gateway at once.
		deploy_tls = play("gateway_server/deploy_tls.yml")
		self.assertEqual(["fleet_tls"], deploy_tls["roles"])
		self.assertNotIn("tasks", deploy_tls)
		self.assertNotIn("handlers", deploy_tls)


class TestUsageSurvivesTheBoxRestarting(unittest.TestCase):
	"""Redis persistence has to be true of the RUNNING server, not just the config file.

	Usage counters are the only thing a gateway originates — keys, users, groups and routes are
	projections the control plane can push again, but a token count that is lost is money. They live
	in Redis until the control plane drains them every five minutes, so a Redis without AOF loses up
	to five minutes of billing on any restart.

	It had silently diverged on a live box: the config block was present and correct, its
	`notify: restart redis` had never fired, and every run since reported the file unchanged — so it
	never notified again and Redis ran on the defaults for the life of the box. A gateway with no AOF
	looks exactly like one with AOF until it restarts, which is why nothing surfaced it.
	"""

	def setUp(self):
		# The tasks live in roles/redis, which both the gateway and the gateway store run.
		self.tasks = yaml.safe_load((PLAYBOOKS / "roles/redis/tasks/main.yml").read_text())
		self.names = [t.get("name") for t in self.tasks]

	def test_the_config_file_asks_for_durable_writes(self):
		block = next(t for t in self.tasks if t.get("name") == "redis zero-loss persistence")
		written = block["ansible.builtin.blockinfile"]["block"]
		self.assertIn("appendonly yes", written)
		self.assertIn("appendfsync everysec", written)

	def test_the_running_server_is_reconciled_too(self):
		# The file alone only takes effect on a restart that may never come.
		self.assertIn("enforce persistence on the running redis", self.names)

	def test_it_does_not_depend_on_a_handler_firing(self):
		"""The trap that caused this. A handler runs only when its task reports a change, so a
		handler that failed to fire once never fires again — the file is already right."""
		block = next(t for t in self.tasks if t.get("name") == "redis zero-loss persistence")
		self.assertNotIn("notify", block)

	def test_redis_is_running_before_anything_talks_to_it(self):
		# redis-cli needs a live server. This task used to sit near the end of the play.
		self.assertLess(
			self.names.index("redis started"),
			self.names.index("enforce persistence on the running redis"),
		)

	def test_it_reconciles_without_restarting_redis(self):
		# A restart drops the pushed keys and routes, so every caller 401s until the next sync.
		# CONFIG SET enables AOF in place.
		enforce = next(t for t in self.tasks if t.get("name") == "enforce persistence on the running redis")
		script = enforce["ansible.builtin.shell"]
		self.assertIn("config set", script)
		self.assertNotIn("systemctl restart", script)
		self.assertNotIn("state: restarted", script)

	def test_it_reports_changed_only_when_it_changed_something(self):
		enforce = next(t for t in self.tasks if t.get("name") == "enforce persistence on the running redis")
		self.assertIn("changed_when", enforce)


class TestOpenRestyIsGoneFromTheseBoxes(unittest.TestCase):
	"""The inference boxes still run it — these two do not, and the plays have to say so."""

	def test_neither_play_installs_openresty(self):
		for name in ("gateway_server/gateway.yml", "ingress_server/ingress.yml"):
			with self.subTest(name):
				self.assertNotIn("openresty", play(name)["roles"])

	def test_both_provision_plays_take_the_ports_back(self):
		# A box provisioned before the cutover still has OpenResty on :80 and :443. Stopping it IS
		# the cutover, and has to be non-fatal for a box that never had it.
		for name in ("gateway_server/gateway.yml", "ingress_server/ingress.yml"):
			with self.subTest(name):
				stop = next(t for t in tasks(name) if t.get("name") == "stop and disable openresty")
				self.assertEqual("stopped", stop["ansible.builtin.systemd"]["state"])
				self.assertFalse(stop["ansible.builtin.systemd"]["enabled"])
				self.assertFalse(stop["failed_when"])

	def test_no_play_here_references_lua_or_an_nginx_config(self):
		for name in PLAYS + ("gateway_server/deploy_tls.yml",):
			with self.subTest(name):
				body = (PLAYBOOKS / name).read_text()
				for dead in ("lua", "nginx.conf", "openresty -t", "opm get"):
					self.assertNotIn(dead, body)


def directives(unit_text):
	"""The unit's actual settings, comments excluded.

	Substring matching against the whole file is what let an earlier version of this pass against a
	COMMENT that said "no Type=notify" — the assertion read as prose and no one noticed.
	"""
	settings = {}
	for line in unit_text.splitlines():
		line = line.strip()
		if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
			continue
		key, _, value = line.partition("=")
		settings[key] = value
	return settings


class TestTheUnitCanSurviveAnUpgrade(unittest.TestCase):
	def setUp(self):
		self.unit = (PLAYBOOKS / "gateway_server/systemd/pathway.service").read_text()
		self.settings = directives(self.unit)

	def test_systemd_follows_the_child_pid(self):
		"""An upgrade replaces the process, and systemd has to be told which one took over.

		The PID file does it: systemd re-reads it when the main process exits and adopts whatever
		pid is in it. That is tableflip's documented integration and it was verified against systemd
		directly — three consecutive upgrades, MainPID tracking the file each time — which is why
		there is no sd_notify handshake here to go wrong.
		"""
		self.assertEqual("/run/pathway/pathway.pid", self.settings["PIDFile"])
		self.assertNotIn("Type", self.settings)
		self.assertNotIn("NotifyAccess", self.settings)

	def test_the_pid_file_is_the_one_the_binary_writes(self):
		# They drift and the handover breaks silently: systemd keeps pointing at the exited
		# process, so the next reload signals nothing.
		for name in PLAYS:
			with self.subTest(name):
				self.assertEqual(self.settings["PIDFile"], agent_env(name)["GROVE_PID_FILE"])

	def test_reload_is_the_upgrade_signal(self):
		self.assertEqual("/bin/kill -HUP $MAINPID", self.settings["ExecReload"])

	def test_stop_outlasts_the_drain(self):
		# The drain defaults to 630s plus a lame-duck window; a shorter TimeoutStopSec SIGKILLs a
		# process shutting down correctly, mid-stream.
		self.assertGreater(int(self.settings["TimeoutStopSec"]), 630)

	def test_it_can_bind_the_privileged_ports(self):
		self.assertEqual("CAP_NET_BIND_SERVICE", self.settings["AmbientCapabilities"])

	def test_both_planes_run_the_same_unit_but_for_the_description(self):
		ingress = (PLAYBOOKS / "ingress_server/systemd/pathway.service").read_text()
		strip = lambda text: [l for l in text.splitlines() if not l.startswith("Description=")]
		self.assertEqual(strip(self.unit), strip(ingress))


class TestTheProcessIsNotRoot(unittest.TestCase):
	"""pathway runs as the service account grove_user creates. Everything it must write — its pid
	file, its logs — is therefore handed to it explicitly, and a box whose unit still runs root is
	refused a deploy rather than restarted into a crash loop."""

	def setUp(self):
		self.unit = directives((PLAYBOOKS / "gateway_server/systemd/pathway.service").read_text())

	def test_it_runs_as_the_service_account(self):
		self.assertEqual("frappe", self.unit["User"])
		self.assertEqual("frappe", self.unit["Group"])

	def test_the_pid_file_lives_in_a_directory_the_process_may_write(self):
		# tableflip writes it via a temp file + rename in that directory, and /run is root's. Without
		# RuntimeDirectory the process exits at Ready() and the unit crash-loops.
		self.assertEqual("pathway", self.unit["RuntimeDirectory"])
		self.assertTrue(self.unit["PIDFile"].startswith(f"/run/{self.unit['RuntimeDirectory']}/"))

	def test_it_may_still_bind_the_privileged_ports(self):
		self.assertEqual("CAP_NET_BIND_SERVICE", self.unit["AmbientCapabilities"])

	def test_the_logs_belong_to_it_including_the_ones_root_wrote_before(self):
		for name in ("gateway_server/gateway.yml", "ingress_server/ingress.yml"):
			with self.subTest(name):
				logs = next(t for t in tasks(name) if t.get("name") == "pathway log directory")
				self.assertEqual("{{ grove_user }}", logs["ansible.builtin.file"]["owner"])
				# Ownership by chown, never `file: recurse`: that stamped the directory's 0755 onto
				# payload.log — customer content, world-readable — the first time it ran.
				self.assertNotIn("recurse", logs["ansible.builtin.file"])
				handover = next(t for t in tasks(name) if t.get("name") == "hand the existing logs to the process")
				self.assertIn("chown -Rc {{ grove_user }}", handover["ansible.builtin.command"])
				private = next(t for t in tasks(name) if t.get("name") == "keep the logs private")
				self.assertIn("chmod -c 0640", private["ansible.builtin.command"])
				self.assertEqual("grove_user", play(name)["roles"][0])

	def test_logrotate_recreates_the_files_for_it(self):
		for name in ("gateway_server/logrotate/pathway", "ingress_server/logrotate/pathway"):
			with self.subTest(name):
				text = (PLAYBOOKS / name).read_text()
				self.assertNotIn("root root", text)
				self.assertIn("create 0640 frappe frappe", text)
				self.assertIn("su frappe frappe", text)

	def test_a_deploy_refuses_a_box_whose_unit_still_runs_root(self):
		# deploy_agent ships no unit but does write agent.env, whose pid path only exists under the
		# new unit's RuntimeDirectory. Checked before any role runs.
		for name in ("gateway_server/deploy_agent.yml", "ingress_server/deploy_agent.yml"):
			with self.subTest(name):
				pre = play(name)["pre_tasks"]
				probe = next(t for t in pre if "RuntimeDirectory=pathway" in str(t))
				self.assertFalse(probe["failed_when"])
				guard = next(t for t in pre if "ansible.builtin.assert" in t)
				self.assertIn("unit_cutover.rc == 0", guard["ansible.builtin.assert"]["that"])


class TestAGatewayRunsNoRedisOfItsOwn(unittest.TestCase):
	"""A gateway keeps everything on its Network's Gateway Store; a Redis still running beside it
	would hold stale keys nothing reads."""

	def test_the_gateway_play_installs_no_redis(self):
		roles = [r.get("role") if isinstance(r, dict) else r for r in play("gateway_server/gateway.yml")["roles"]]
		self.assertNotIn("redis", roles)

	def test_the_old_redis_stops_only_after_the_gateway_restarted_off_it(self):
		names = [task.get("name") for task in tasks("gateway_server/gateway.yml")]
		self.assertLess(
			names.index("restart onto the new agent.env"),
			names.index("stop the redis a gateway once ran itself"),
		)


class TestAStoreIsNeverOpenWithoutAPassword(unittest.TestCase):
	"""A store listens on its private address, and holds every gateway key hash in the Network."""

	def test_the_store_play_refuses_a_blank_password(self):
		[check] = play("gateway_store/store.yml")["pre_tasks"]
		self.assertIn("redis_password", " ".join(check["ansible.builtin.assert"]["that"]))

	def test_the_listen_address_is_only_written_with_a_password(self):
		redis = yaml.safe_load((PLAYBOOKS / "roles/redis/tasks/main.yml").read_text())
		network = next(task for task in redis if task.get("name") == "redis listens for the gateways")
		self.assertIn("requirepass", network["ansible.builtin.blockinfile"]["block"])
		self.assertIn("redis_password", network["when"])
		self.assertTrue(network["no_log"])


class TestMaintenanceIsATunable(unittest.TestCase):
	"""Maintenance lives in config.json, so a box restarted in it comes back in it, and it moves with
	a signal rather than a restart."""

	def test_the_key_is_rendered_only_while_on(self):
		# A pathway older than the key refuses the file whole, so a box not in maintenance must get the
		# bytes it always did.
		template = (PLAYBOOKS / "gateway_server/config.json.j2").read_text()
		self.assertRegex(template, r'{% if gateway_maintenance \| default\(false\) %}\n\s*"maintenance": true,\n{% endif %}')

	def test_the_config_play_signals_and_touches_nothing_else(self):
		for name in ("gateway_server/config.yml", "ingress_server/config.yml"):
			with self.subTest(name):
				config = play(name)
				# The handler runs as the play ends, so the play's rc already covers the signal.
				self.assertEqual(["config.json"], [task["name"] for task in config["tasks"]])
				self.assertEqual("reload pathway config", config["tasks"][0]["notify"])
				[handler] = config["handlers"]
				self.assertIn("SIGUSR1", handler["ansible.builtin.command"])


