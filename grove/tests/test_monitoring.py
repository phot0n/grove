# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What a Monitoring Agent is told to scrape. Pure — shapes the http_sd payload and renders
the agent's scrape config, no site and no box.

The join that makes any of this useful is `engine` == the deployment's engine_url, verbatim.
A reformatted value still produces series, so nothing fails loudly — the metrics simply stop
matching the routes they describe, which is why it is asserted here.
"""

import json
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

import frappe
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from passlib.hash import sha256_crypt

from grove import monitoring
from grove.grove.doctype.grove_settings.grove_settings import SD_TOKEN_PATTERN
from grove.grove.doctype.monitoring_agent.monitoring_agent import MonitoringAgent
from grove.monitoring import (
	BOX_HTTPS_PORT,
	DCGM_EXPORTER_PORT,
	GPU_METRICS_PATH,
	NODE_EXPORTER_PORT,
	NODE_METRICS_PATH,
	VMAGENT_PORT,
	build_host_targets,
	engine_entry,
)

PLAYBOOKS = Path(__file__).parent.parent.parent / "playbooks"
AGENT = PLAYBOOKS / "monitoring_agent"
INFERENCE = PLAYBOOKS / "inference_server"
# The exporters go on every box whoever owns it, so they live in the shared roles folder
# rather than any one doctype's.
SHARED_ROLES = PLAYBOOKS / "roles"


def role_dir(name):
	"""Where a role lives — the shared folder, else the play's own. The places Ansible itself
	looks. engine_proxy is inference-only (a proxy box already serves :80 from OpenResty), so it
	is not shared, but what it publishes is half of what the agent is told to scrape."""
	for candidate in (SHARED_ROLES / name, AGENT / "roles" / name, INFERENCE / "roles" / name):
		if candidate.is_dir():
			return candidate
	raise AssertionError(f"no role named {name}")


VMAGENT_ROLE = role_dir("vmagent")

TEMPLATES = Environment(
	loader=FileSystemLoader(VMAGENT_ROLE / "templates"),
	trim_blocks=True,
	keep_trailing_newline=True,
)


def box(
	name,
	ip="10.0.0.5",
	machine="MACHINE-1",
	region="ap-south-1",
	has_gpu=False,
	private_ip="",
	network="",
):
	return {
		"name": name,
		"ip": ip,
		"machine": machine,
		"region": region,
		"has_gpu": has_gpu,
		"private_ip": private_ip,
		"network": network,
	}


class TestHostTargets(unittest.TestCase):
	def test_every_box_exports_node_metrics(self):
		[entry] = build_host_targets([box("PROXY-1")])
		self.assertEqual(entry["targets"], ["10.0.0.5:443"])
		self.assertEqual(entry["labels"]["__metrics_path__"], NODE_METRICS_PATH)
		self.assertEqual(entry["labels"]["machine"], "MACHINE-1")
		self.assertEqual(entry["labels"]["region"], "ap-south-1")
		self.assertEqual(entry["labels"]["server"], "PROXY-1")

	def test_a_box_is_scraped_through_its_tls_front_not_the_exporter_port(self):
		# The exporters still listen on 9100/9400; they are simply no longer reachable from
		# outside, so a target naming those ports could only ever be down.
		[entry] = build_host_targets([box("PROXY-1")])
		self.assertEqual(entry["labels"]["__scheme__"], "https")
		self.assertNotIn(str(NODE_EXPORTER_PORT), entry["targets"][0])

	def test_a_gpu_box_also_exports_dcgm(self):
		entries = build_host_targets([box("INF-1", has_gpu=True)])
		self.assertEqual([e["targets"][0] for e in entries], ["10.0.0.5:443", "10.0.0.5:443"])
		self.assertEqual(
			[e["labels"]["__metrics_path__"] for e in entries], [NODE_METRICS_PATH, GPU_METRICS_PATH]
		)

	def test_each_exporter_on_a_box_keeps_its_own_instance(self):
		# Both now resolve to the same address, so left to default they would collapse into one
		# `instance` and any `by (instance)` query would silently merge node with GPU. The value
		# keeps the exporter's own port, which is what these targets reported before they moved.
		entries = build_host_targets([box("INF-1", has_gpu=True)])
		self.assertEqual(
			[e["labels"]["instance"] for e in entries], ["10.0.0.5:9100", "10.0.0.5:9400"]
		)

	def test_a_box_without_gpus_has_no_dcgm_target(self):
		# DCGM is only installed on GPU boxes — a target for it elsewhere could only be down.
		entries = build_host_targets([box("PROXY-1")])
		self.assertEqual(len(entries), 1)

	def test_a_box_with_no_address_is_skipped(self):
		# A Machine that has not been provisioned yet has no IP to scrape.
		self.assertEqual(build_host_targets([box("INF-1", ip="")]), [])


class TestBoxesAreScrapedPrivatelyWhereTheyCanBe(unittest.TestCase):
	"""A collector is local to its region, so a public-IP scrape leaves the VPC and comes back,
	and is billed for it. The private address is used when it routes — which is a question about
	the Network (one VPC, one subnet, one AZ), not the region: two boxes in one region but
	different Networks have no route between their private addresses, and a target that cannot be
	reached reads exactly like a box that just died."""

	SAME = box("INF-1", private_ip="172.31.0.9", network="NET-mumbai")

	def test_a_box_in_the_agents_network_is_scraped_privately(self):
		[entry] = build_host_targets([self.SAME], "NET-mumbai")
		self.assertEqual(entry["targets"], ["172.31.0.9:443"])

	def test_a_box_in_another_network_is_scraped_publicly(self):
		# Same region is not enough — a different Network is a different VPC, and the private
		# address has no route from here.
		[entry] = build_host_targets([self.SAME], "NET-singapore")
		self.assertEqual(entry["targets"], ["10.0.0.5:443"])

	def test_a_box_with_no_private_address_is_scraped_publicly(self):
		# Colo and bare metal — the blackwell box has no private address to be reached by.
		colo = box("INF-colo", network="NET-mumbai")
		[entry] = build_host_targets([colo], "NET-mumbai")
		self.assertEqual(entry["targets"], ["10.0.0.5:443"])

	def test_an_agent_in_no_network_scrapes_everything_publicly(self):
		[entry] = build_host_targets([self.SAME], "")
		self.assertEqual(entry["targets"], ["10.0.0.5:443"])

	def test_the_address_moves_but_the_series_does_not(self):
		# `instance` is which exporter this is, not where it was reached. A box that gains a
		# private IP would otherwise rename every series it has ever reported, and history would
		# stop joining to the present.
		public = build_host_targets([self.SAME], "")
		private = build_host_targets([self.SAME], "NET-mumbai")
		self.assertNotEqual(public[0]["targets"], private[0]["targets"])
		self.assertEqual(public[0]["labels"], private[0]["labels"])
		self.assertEqual(private[0]["labels"]["instance"], "10.0.0.5:9100")

	def test_both_exporters_on_a_box_move_together(self):
		entries = build_host_targets(
			[box("INF-1", has_gpu=True, private_ip="172.31.0.9", network="NET-mumbai")], "NET-mumbai"
		)
		self.assertEqual([e["targets"][0] for e in entries], ["172.31.0.9:443", "172.31.0.9:443"])
		self.assertEqual(
			[e["labels"]["instance"] for e in entries], ["10.0.0.5:9100", "10.0.0.5:9400"]
		)

	def test_an_engine_on_a_private_box_keeps_its_public_identity(self):
		# The `engine` label is the join back to the route agent_sync pushes, and `instance` is
		# what tells two engines on one box apart. Neither may follow the address.
		entry = engine_entry(
			"https://10.0.0.5/e/md-00007", {"deployment": "MD-00007"}, address="172.31.0.9"
		)
		self.assertEqual(entry["targets"], ["172.31.0.9:443"])
		self.assertEqual(entry["labels"]["engine"], "https://10.0.0.5/e/md-00007")
		self.assertEqual(entry["labels"]["instance"], "https://10.0.0.5/e/md-00007")
		self.assertEqual(entry["labels"]["__metrics_path__"], "/e/md-00007/metrics")

	def test_a_pod_is_never_scraped_privately(self):
		# A pod has no Machine and no Network — there is no private address to prefer, and
		# engine_targets passes none.
		entry = engine_entry("http://1.2.3.4:8081", {"deployment": "POD-1"})
		self.assertEqual(entry["targets"], ["1.2.3.4:8081"])


class TestWhichBoxesAreScraped(unittest.TestCase):
	"""Which servers become host targets at all. A terminated box was still being scraped —
	13.207.153.238 sat in a live agent's list with the machine long gone — and a target that can
	only ever be down is worse than no target, because it reads like a box that just died."""

	def filters_for(self, boxes):
		captured = {}

		def get_all(doctype, filters=None, **kwargs):
			captured[doctype] = filters
			return []

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			boxes("MA-1")
		return captured

	def test_a_terminated_inference_server_is_not_a_target(self):
		# inference_boxes also reads Machine GPU for the has_gpu flag, so key on the doctype.
		filters = self.filters_for(monitoring.inference_boxes)["Inference Server"]
		self.assertEqual(filters["status"], ("!=", "Terminated"))

	def test_a_terminated_front_box_is_not_a_target_either(self):
		# front_boxes runs the same query over Gateway Server and Ingress Server — both run the
		# same OpenResty over the same node_exporter, and both leave a dead target behind.
		captured = self.filters_for(monitoring.front_boxes)
		for doctype in ("Gateway Server", "Ingress Server"):
			with self.subTest(doctype):
				self.assertEqual(captured[doctype]["status"], ("!=", "Terminated"))

	def test_a_broken_box_is_still_scraped(self):
		# Only Terminated is excluded. A Broken box still exists, and its metrics are the fastest
		# way to find out what is wrong with it — filtering on == "Active" would blind you there.
		for boxes, doctype in (
			(monitoring.inference_boxes, "Inference Server"),
			(monitoring.front_boxes, "Gateway Server"),
			(monitoring.front_boxes, "Ingress Server"),
		):
			with self.subTest(doctype):
				self.assertNotIn("Active", str(self.filters_for(boxes)[doctype]["status"]))


class TestEngineTargets(unittest.TestCase):
	LABELS = {
		"model": "qwen3-35b",
		"deployment": "MD-00007",
		"server": "INF-blackwell",
		"machine": "MACHINE-1",
		"region": "ap-south-1",
	}

	def test_a_deployment_is_scraped_through_its_boxs_engine_proxy(self):
		entry = engine_entry("https://10.0.0.9/e/md-00007", self.LABELS)
		self.assertEqual(entry["targets"], ["10.0.0.9:443"])
		self.assertEqual(entry["labels"]["__metrics_path__"], "/e/md-00007/metrics")
		self.assertEqual(entry["labels"]["engine"], "https://10.0.0.9/e/md-00007")
		self.assertEqual(entry["labels"]["deployment"], "MD-00007")

	def test_a_pod_is_still_scraped_on_its_own_port(self):
		# A pod IS the vLLM container — no box, no Ansible, nothing to put a front in front of.
		# One derivation has to serve both shapes, which is why nothing here hardcodes :443.
		entry = engine_entry("http://10.0.0.9:8081", self.LABELS)
		self.assertEqual(entry["targets"], ["10.0.0.9:8081"])
		self.assertEqual(entry["labels"]["__metrics_path__"], "/metrics")
		self.assertEqual(entry["labels"]["engine"], "http://10.0.0.9:8081")

	def test_every_engine_on_a_box_keeps_its_own_instance(self):
		# Two deployments on one box share an address now; the default `instance` would name the
		# box and merge them.
		first = engine_entry("https://10.0.0.9/e/md-00007", self.LABELS)
		second = engine_entry("https://10.0.0.9/e/md-00008", self.LABELS)
		self.assertEqual(first["targets"], second["targets"])
		self.assertNotEqual(first["labels"]["instance"], second["labels"]["instance"])

	def test_an_engine_without_a_url_is_not_a_target(self):
		# Mid-provision deployments and still-loading pods hold "" — the same filter
		# agent_sync applies to routes.
		self.assertIsNone(engine_entry("", self.LABELS))
		self.assertIsNone(engine_entry(None, self.LABELS))

	def test_the_scheme_travels_with_the_target(self):
		entry = engine_entry("https://pod.example.net/", {"model": "m", "deployment": "POD-1"})
		self.assertEqual(entry["targets"], ["pod.example.net:443"])
		self.assertEqual(entry["labels"]["__scheme__"], "https")
		# A trailing slash must not become //metrics — the path is built by concatenation.
		self.assertEqual(entry["labels"]["__metrics_path__"], "/metrics")

	def test_a_pod_carries_no_box_labels(self):
		# A pod is not on a box we own — machine/region here would name the scraper's host.
		entry = engine_entry("http://1.2.3.4:8080", {"model": "m", "deployment": "POD-1"})
		self.assertNotIn("machine", entry["labels"])
		self.assertNotIn("region", entry["labels"])

	def test_a_missing_label_is_empty_not_null(self):
		# get_all hands back None for an unset Link; None would serialise as null and vmagent
		# rejects a non-string label value.
		entry = engine_entry("http://1.2.3.4:8080", {"model": None, "deployment": "POD-1"})
		self.assertEqual(entry["labels"]["model"], "")


class TestExporterPortsAgree(unittest.TestCase):
	"""The exporters listen on ports their roles choose; Grove hands the agent those same
	ports as targets. Nothing at runtime notices a mismatch — the agent just scrapes a closed
	port and reports the box down, which reads identically to a dead box."""

	def role_default(self, role, key):
		return yaml.safe_load((role_dir(role) / "defaults/main.yml").read_text())[key]

	def test_the_gpu_exporter_is_scraped_where_it_listens(self):
		self.assertEqual(self.role_default("dcgm_exporter", "dcgm_exporter_port"), DCGM_EXPORTER_PORT)

	def test_the_node_exporter_is_scraped_where_it_listens(self):
		self.assertEqual(self.role_default("node_exporter", "node_exporter_port"), NODE_EXPORTER_PORT)

	def test_the_agent_is_scraped_where_it_listens(self):
		# The agent scrapes its own /metrics, so this port is both what it serves on and what
		# Grove hands back to it as a target.
		listen = self.role_default("vmagent", "vmagent_listen_address")
		self.assertEqual(int(listen.rsplit(":", 1)[1]), VMAGENT_PORT)

	def test_the_agent_box_installs_the_node_exporter_it_will_be_asked_to_scrape(self):
		# Nothing else installs one there: the agent box carries no Inference/Gateway Server doc.
		play = yaml.safe_load((AGENT / "agent.yml").read_text())[0]
		self.assertIn("node_exporter", play["roles"])

	def test_the_exporters_are_republished_where_grove_says_they_are(self):
		# The port stopped being the address: a box answers on 443 and the path is what picks the
		# exporter. Same failure the port pair guards — a mismatch scrapes a 404, which reads
		# exactly like a box that is down.
		self.assertEqual(
			self.role_default("engine_proxy", "engine_proxy_node_metrics_path"), NODE_METRICS_PATH
		)
		self.assertEqual(
			self.role_default("engine_proxy", "engine_proxy_gpu_metrics_path"), GPU_METRICS_PATH
		)

	def test_the_front_forwards_to_the_ports_the_exporters_listen_on(self):
		# engine_proxy restates the two ports because serve.yml runs it without the exporter
		# roles, so their defaults are not in scope. Restated, therefore asserted.
		for key, port in (
			("engine_proxy_node_upstream", NODE_EXPORTER_PORT),
			("engine_proxy_gpu_upstream", DCGM_EXPORTER_PORT),
		):
			with self.subTest(key):
				self.assertEqual(self.role_default("engine_proxy", key), f"127.0.0.1:{port}")

	def test_the_front_listens_where_grove_addresses_the_box(self):
		self.assertEqual(self.role_default("engine_proxy", "engine_proxy_port"), BOX_HTTPS_PORT)

	def test_the_agent_authenticates_as_the_user_the_boxes_hash(self):
		# The username is a constant in two roles rather than a Grove Settings field, because a
		# settable one only added a way for the two ends to disagree — and it did: the Single doc
		# predated the field, so its default never applied and the htpasswd rendered with a blank
		# user. Constants can still drift apart, so they are pinned to each other here.
		self.assertEqual(
			self.role_default("grove_https", "scrape_username"),
			self.role_default("vmagent", "monitoring_scrape_username"),
		)


class TestServiceDiscoveryAuth(unittest.TestCase):
	"""`targets` is guest-whitelisted, so this token is the whole of what stands between the
	internet and the fleet's inventory — every box's address and open exporter ports, every
	model and engine URL.

	frappe.throw needs a bound site, so what is asserted here is that the call does not return:
	whether it raises AuthenticationError or something else, it did not hand over the list."""

	def authenticate(self, supplied, configured, user="Guest", may_read=False):
		settings = unittest.mock.Mock()
		settings.get_password.return_value = configured
		session = unittest.mock.Mock()
		session.user = user
		with (
			unittest.mock.patch.object(frappe, "get_cached_doc", return_value=settings),
			unittest.mock.patch.object(frappe, "session", session),
			unittest.mock.patch.object(frappe, "has_permission", return_value=may_read),
		):
			monitoring.authenticate(supplied)

	def test_a_signed_in_reader_needs_no_token(self):
		# The form's Show Targets button, which sends no token — and should not have to.
		self.authenticate("", None, user="Administrator", may_read=True)

	def test_a_signed_in_user_who_cannot_read_the_agent_still_needs_one(self):
		with self.assertRaises(Exception):
			self.authenticate("", "s3cr3t", user="someone@example.com", may_read=False)

	def test_the_matching_token_is_let_through(self):
		self.authenticate("s3cr3t", "s3cr3t")

	def test_a_wrong_token_is_refused(self):
		with self.assertRaises(Exception):
			self.authenticate("guess", "s3cr3t")

	def test_no_token_at_all_is_refused(self):
		with self.assertRaises(Exception):
			self.authenticate("", "s3cr3t")

	def test_the_accepted_token_charset_survives_a_url_unescaped(self):
		# Grove Settings rejects anything else, which is what lets the scrape config interpolate
		# the secret straight into a query string.
		for token in ("0f3a-b21c_9d.e~4a7b", "A" * 16):
			with self.subTest(token):
				self.assertTrue(SD_TOKEN_PATTERN.match(token))
				self.assertEqual(quote(token, safe=""), token)
		for token in ("short", "has/slash/in/it!!", "has&ampersand&here", "has#hash#here123"):
			with self.subTest(token):
				self.assertIsNone(SD_TOKEN_PATTERN.match(token))

	def test_an_unset_token_refuses_everyone_rather_than_waving_them_through(self):
		# The one that matters: a blank field must not turn into "any token matches", which is
		# what a plain equality check between two empty strings would do.
		for supplied in ("", "anything"):
			with self.subTest(supplied), self.assertRaises(Exception):
				self.authenticate(supplied, None)


class TestScrapePasswordHash(unittest.TestCase):
	"""What each box's htpasswd file carries. Derived in the control plane so the password never
	reaches a box and never lands in an Ansible argv — press runs `htpasswd -Bbc` on the box
	instead, which /proc and the Ansible Task doc both record."""

	def hash_for(self, password, stored=""):
		from grove.grove.doctype.grove_settings.grove_settings import GroveSettings

		settings = unittest.mock.Mock(spec=["get_password", "scrape_password_hash"])
		settings.get_password.return_value = password
		settings.scrape_password_hash = stored
		GroveSettings.set_scrape_password_hash(settings)
		return settings.scrape_password_hash

	def test_nginx_can_verify_what_grove_wrote(self):
		hashed = self.hash_for("sc4ape")
		self.assertTrue(sha256_crypt.verify("sc4ape", hashed))
		self.assertFalse(sha256_crypt.verify("guess", hashed))

	def test_the_password_itself_is_never_in_it(self):
		self.assertNotIn("sc4ape", self.hash_for("sc4ape"))

	def test_an_unchanged_password_keeps_the_same_hash(self):
		# The whole reason the hash is stored rather than computed per play: sha256_crypt salts
		# randomly, so re-hashing on every save would rewrite the file on every box in the fleet.
		first = self.hash_for("sc4ape")
		self.assertEqual(self.hash_for("sc4ape", stored=first), first)

	def test_a_changed_password_replaces_it(self):
		first = self.hash_for("sc4ape")
		self.assertNotEqual(self.hash_for("different", stored=first), first)

	def test_a_corrupt_hash_is_replaced_rather_than_raised_on(self):
		# The field is read-only, so anything unparseable in it came from an edit that should not
		# survive the next save — and passlib raises rather than returning False.
		self.assertTrue(sha256_crypt.verify("sc4ape", self.hash_for("sc4ape", stored="nonsense")))

	def test_clearing_the_password_clears_the_hash(self):
		self.assertEqual(self.hash_for("", stored=self.hash_for("sc4ape")), "")


class TestTheAgentPlayChecksBeforeItInstalls(unittest.TestCase):
	"""agent.yml installs node_exporter before vmagent. A requirement checked inside the vmagent
	role therefore fails with node_exporter already on the box — a half-built machine and a play
	log to read it out of. The checks belong ahead of every role."""

	PLAY = yaml.safe_load((AGENT / "agent.yml").read_text())[0]

	def test_every_setting_the_play_needs_is_asserted_before_any_role_runs(self):
		asserted = yaml.dump(self.PLAY["pre_tasks"])
		for variable in (
			"monitoring_remote_write_url",
			"monitoring_remote_write_token",
			"monitoring_sd_url",
			"monitoring_sd_token",
			"monitoring_agent",
			# Without this the agent installs happily and is refused by every host target,
			# which reads as a fleet that is simply down.
			"monitoring_scrape_password",
		):
			with self.subTest(variable):
				self.assertIn(variable, asserted)

	def test_no_role_defers_a_check_until_after_something_is_installed(self):
		# Every task file, not just main.yml — vmagent's config half lives in config.yml so
		# update_config can re-run it, and a check hidden there installs just as much first.
		for role in self.PLAY["roles"]:
			for task_file in sorted((role_dir(role) / "tasks").glob("*.yml")):
				with self.subTest(f"{role}/{task_file.name}"):
					tasks = yaml.safe_load(task_file.read_text())
					self.assertNotIn(
						"ansible.builtin.assert", {key for task in tasks for key in task}
					)


class TestEveryTemplateVariableHasADefault(unittest.TestCase):
	"""A template variable no role defaults is an AnsibleUndefinedVariable at run time — on the
	box, mid-play, after the earlier tasks already changed it. Rendering each role's templates
	against its own defaults alone catches it here instead: the extra-vars Grove passes only ever
	override names `defaults/main.yml` already declares."""

	def test_each_role_defaults_every_variable_its_templates_use(self):
		# engine_proxy joins the monitoring roles here because it is what publishes them, and it
		# is configured the same way — by its defaults, with Grove overriding names they already
		# declare. The vllm role beside it is not: it is driven entirely by extra-vars assembled
		# from the doctypes, and test_engine_templates.py renders it against those instead.
		roles = [*SHARED_ROLES.iterdir(), *(AGENT / "roles").iterdir(), INFERENCE / "roles/engine_proxy"]
		# engine_proxy's config names paths that grove_https (certificate, htpasswd) and openresty
		# (log directory) own, and both always run ahead of it in every play that uses it.
		# Rendering it against its own defaults alone would demand a second copy of those paths,
		# which is the drift this test exists to prevent.
		shared = {}
		for owner in ("grove_https", "openresty"):
			shared.update(yaml.safe_load((SHARED_ROLES / owner / "defaults/main.yml").read_text()) or {})
		for role in sorted(path.name for path in roles if (path / "templates").is_dir()):
			defaults = yaml.safe_load((role_dir(role) / "defaults/main.yml").read_text()) or {}
			environment = Environment(
				loader=FileSystemLoader(role_dir(role) / "templates"), undefined=StrictUndefined
			)
			for template in environment.list_templates():
				with self.subTest(f"{role}/{template}"):
					environment.get_template(template).render(**{**shared, **defaults})


class TestScrapeConfig(unittest.TestCase):
	"""vmagent parses this file strictly, and it is the whole of what the agent knows."""

	BASE = {
		"monitoring_scrape_interval": "15s",
		"monitoring_engine_scrape_interval": "5s",
		"monitoring_sd_url": "https://grove.example/api/method/grove.monitoring.targets",
		"monitoring_sd_refresh_interval": "30s",
		"monitoring_agent": "MA-1",
		"monitoring_sd_token": "0f3a-b21c_9d.e~4a7b",
		"vmagent_config_dir": "/etc/vmagent",
		"monitoring_static_targets": False,
		"monitoring_scrape_username": "grove",
		"monitoring_scrape_password": "sc4ape",
	}

	def setUp(self):
		self.config = yaml.safe_load(TEMPLATES.get_template("scrape.yml.j2").render(**self.BASE))
		self.jobs = {job["job_name"]: job for job in self.config["scrape_configs"]}

	def test_every_target_is_reached_over_tls_it_cannot_verify(self):
		# Each box fronts itself with a self-signed certificate, so a verifying scrape would fail
		# on every target. This encrypts the hop; it authenticates nothing.
		for job in self.jobs.values():
			self.assertTrue(job["tls_config"]["insecure_skip_verify"])

	def test_only_the_host_job_authenticates(self):
		# /metrics/node and /metrics/gpu are behind basic auth because an exporter answers whoever
		# reaches it. An engine's /metrics is inside its own /e/<slug>/ location, guarded by
		# nothing — exactly as it was when the engine port itself was open.
		self.assertEqual(self.jobs["grove-host"]["basic_auth"]["username"], "grove")
		self.assertNotIn("basic_auth", self.jobs["grove-engine"])

	def test_the_scrape_password_is_read_from_a_file_not_the_config(self):
		# /proc/<pid>/cmdline is world-readable and Ansible records its argv on the Ansible Task
		# doc — the same reason the remote-write token is a file.
		auth = self.jobs["grove-host"]["basic_auth"]
		self.assertEqual(auth["password_file"], "/etc/vmagent/scrape.password")
		self.assertNotIn("password", auth)
		self.assertNotIn(self.BASE["monitoring_scrape_password"], yaml.dump(self.config))

	def test_the_password_file_is_not_world_readable(self):
		tasks = yaml.safe_load((VMAGENT_ROLE / "tasks/config.yml").read_text())
		[write] = [
			task for task in tasks
			if task.get("ansible.builtin.copy", {}).get("dest", "").endswith("scrape.password")
		]
		self.assertEqual(write["ansible.builtin.copy"]["mode"], "0600")
		self.assertTrue(write["no_log"])

	def test_both_jobs_discover_from_grove_and_hold_no_targets(self):
		self.assertEqual(set(self.jobs), {"grove-host", "grove-engine"})
		for job in self.jobs.values():
			self.assertNotIn("static_configs", job)
			self.assertNotIn("file_sd_configs", job)
			self.assertIn("kind=", job["http_sd_configs"][0]["url"])

	def test_each_job_asks_for_its_own_agent(self):
		for job in self.jobs.values():
			self.assertIn("agent=MA-1", job["http_sd_configs"][0]["url"])

	def test_the_sd_blocks_hold_only_fields_vmagent_accepts(self):
		# vmagent strict-parses this file and dies on an unknown field, then Restart=on-failure
		# turns that into a crash loop that reads as "port refused". It rejects Prometheus's
		# per-config refresh_interval on an http_sd_config: the interval is a unit flag,
		# -promscrape.httpSDCheckInterval, so it must not reappear here.
		for job in self.jobs.values():
			[sd] = job["http_sd_configs"]
			self.assertEqual(set(sd), {"url"})

	def test_the_refresh_interval_reaches_vmagent_as_a_flag(self):
		unit = TEMPLATES.get_template("vmagent.service.j2").render(
			**self.BASE,
			monitoring_extra_labels={},
			monitoring_remote_write_url="http://push.example/v1/write",
			vmagent_listen_address="127.0.0.1:8429",
			vmagent_buffer_path="/var/lib/vmagent",
			vmagent_max_disk_usage="4GiB",
		)
		self.assertIn(f"-promscrape.httpSDCheckInterval={self.BASE['monitoring_sd_refresh_interval']}", unit)

	def test_engines_are_scraped_on_the_tighter_interval(self):
		self.assertEqual(self.jobs["grove-engine"]["scrape_interval"], "5s")
		self.assertEqual(self.config["global"]["scrape_interval"], "15s")

	def test_the_token_travels_in_the_query_string(self):
		# Not an Authorization header: Frappe's validate_auth rejects one it cannot resolve to a
		# user before the endpoint runs, so a bare shared secret can only arrive this way.
		for job in self.jobs.values():
			self.assertIn(f"token={self.BASE['monitoring_sd_token']}", job["http_sd_configs"][0]["url"])
			self.assertNotIn("authorization", job["http_sd_configs"][0])

	def test_a_token_reaches_the_endpoint_byte_for_byte(self):
		# Jinja's urlencode leaves '/' alone, so escaping cannot be relied on to carry an
		# arbitrary secret — SD_TOKEN_PATTERN in Grove Settings is what keeps it URL-safe.
		[url] = [job["http_sd_configs"][0]["url"] for job in [self.jobs["grove-host"]]]
		self.assertEqual(parse_qs(urlparse(url).query)["token"], [self.BASE["monitoring_sd_token"]])

	def test_the_file_holding_the_token_is_not_world_readable(self):
		# The token is in this file now, so its mode is the only thing protecting it on the box.
		tasks = yaml.safe_load((VMAGENT_ROLE / "tasks/targets.yml").read_text())
		[write] = [task for task in tasks if task.get("ansible.builtin.template", {}).get("src") == "scrape.yml.j2"]
		self.assertEqual(write["ansible.builtin.template"]["mode"], "0600")


class TestPushedTargets(unittest.TestCase):
	"""The file_sd half: Grove writes the lists instead of serving them, for a box that cannot
	reach Grove at all. Same entries either way — only how they arrive changes."""

	BASE = {**TestScrapeConfig.BASE, "monitoring_static_targets": True}

	def setUp(self):
		self.config = yaml.safe_load(TEMPLATES.get_template("scrape.yml.j2").render(**self.BASE))
		self.jobs = {job["job_name"]: job for job in self.config["scrape_configs"]}

	def test_both_jobs_read_files_and_never_ask_grove(self):
		self.assertEqual(set(self.jobs), {"grove-host", "grove-engine"})
		for job in self.jobs.values():
			self.assertNotIn("http_sd_configs", job)
			[sd] = job["file_sd_configs"]
			self.assertEqual(set(sd), {"files"})

	def test_each_job_reads_its_own_kind(self):
		self.assertEqual(
			self.jobs["grove-host"]["file_sd_configs"][0]["files"], ["/etc/vmagent/targets_host.json"]
		)
		self.assertEqual(
			self.jobs["grove-engine"]["file_sd_configs"][0]["files"], ["/etc/vmagent/targets_engine.json"]
		)

	def test_the_sd_token_never_reaches_a_box_that_cannot_use_it(self):
		# file_sd fetches nothing, so shipping the shared secret here would spread it for free.
		self.assertNotIn(self.BASE["monitoring_sd_token"], yaml.dump(self.config))

	def test_the_intervals_are_the_same_either_way(self):
		self.assertEqual(self.jobs["grove-engine"]["scrape_interval"], "5s")
		self.assertEqual(self.config["global"]["scrape_interval"], "15s")

	def test_the_lists_are_written_before_the_config_that_reads_them(self):
		# file_sd pointing at a file that is not there yet is a job that starts up down.
		tasks = yaml.safe_load((VMAGENT_ROLE / "tasks/targets.yml").read_text())
		names = [task["name"] for task in tasks]
		self.assertLess(names.index("Write the pushed target lists"), names.index("Write the scrape config"))

	def test_the_lists_are_only_written_when_grove_is_pushing_them(self):
		[write] = [task for task in yaml.safe_load((VMAGENT_ROLE / "tasks/targets.yml").read_text())
			if task["name"] == "Write the pushed target lists"]
		self.assertIn("monitoring_static_targets", write["when"])

	def test_pushing_is_a_dev_site_fallback_and_nothing_else(self):
		# The gate is developer_mode alone — no field to tick, and no way to end up pushing
		# stale snapshots to a production fleet that can pull fresh ones for itself.
		from grove.grove.doctype.monitoring_agent.monitoring_agent import is_pushing_targets

		with unittest.mock.patch.object(frappe, "conf", {"developer_mode": 1}):
			self.assertTrue(is_pushing_targets())
		for off in ({"developer_mode": 0}, {}):
			with unittest.mock.patch.object(frappe, "conf", off):
				self.assertFalse(is_pushing_targets())

	def test_a_pushed_list_is_the_same_payload_http_sd_would_have_served(self):
		# The whole point: file_sd and http_sd read the identical shape, so switching how the
		# list travels cannot change what the agent scrapes.
		entries = build_host_targets([box("INF-1", has_gpu=True)])
		written = json.loads(json.dumps(entries))
		self.assertEqual(written, entries)
		for entry in written:
			self.assertEqual(set(entry), {"targets", "labels"})


class TestTheAgentsOwnTuningReachesItsBox(unittest.TestCase):
	"""What the doc passes to Ansible, rendered the way Ansible resolves it — role defaults
	underneath, extra-vars on top.

	Push Targets re-renders scrape.yml, which carries the intervals as well as the target lists.
	It used to be handed the three target variables alone, so every interval fell back to
	defaults/main.yml and the doc's value was silently written over on a box that was already
	running it."""

	VMAGENT_DEFAULTS = yaml.safe_load((VMAGENT_ROLE / "defaults/main.yml").read_text())

	def agent(self, **fields):
		"""A Monitoring Agent as ansible_variables reads one. The property is called unbound so
		this stays pure — it touches nothing but these attributes."""
		return SimpleNamespace(**{
			"name": "MA-1",
			"remote_write_url": "https://ingest.example/api/v1/write",
			"get_password": lambda *args, **kwargs: "push-token",
			"target_variables": {
				"monitoring_static_targets": True,
				"monitoring_host_targets": [],
				"monitoring_engine_targets": [],
			},
			# Every tuning field unset, the way a fresh agent arrives.
			"scrape_interval": None,
			"engine_scrape_interval": None,
			"flush_interval": None,
			"max_disk_usage": None,
			**fields,
		})

	def variables(self, **fields):
		settings = SimpleNamespace(monitoring_variables={
			"monitoring_extra_labels": {},
			"monitoring_sd_url": "https://grove.example/api/method/grove.monitoring.targets",
			"monitoring_sd_token": "tok",
			"monitoring_scrape_password": "sc4ape",
		})
		with unittest.mock.patch.object(frappe, "get_single", return_value=settings):
			return MonitoringAgent.ansible_variables.fget(self.agent(**fields))

	def scrape_config(self, **fields):
		"""scrape.yml as the box receives it: the role's defaults, with the doc's extra-vars over
		them — the precedence Ansible applies, and the whole question here."""
		rendered = TEMPLATES.get_template("scrape.yml.j2").render(
			**{**self.VMAGENT_DEFAULTS, **self.variables(**fields)}
		)
		return yaml.safe_load(rendered)

	def test_the_docs_interval_beats_the_role_default(self):
		# The bug, from the operator's side: set 7s, and 7s is what the box scrapes on.
		config = self.scrape_config(engine_scrape_interval="7s", scrape_interval="20s")
		[engine] = [j for j in config["scrape_configs"] if j["job_name"] == "grove-engine"]
		self.assertEqual(engine["scrape_interval"], "7s")
		self.assertEqual(config["global"]["scrape_interval"], "20s")

	def test_push_targets_carries_the_same_tuning_as_an_install(self):
		# The play that had this wrong. Its extra-vars have to cover everything the tasks it runs
		# render, not just the lists that give the play its name.
		played = {}

		def run_playbook(playbook, extravars=None, **kwargs):
			played.update(playbook=playbook, extravars=extravars)
			return ("PLAY-1", 0)

		agent = self.agent(engine_scrape_interval="7s")
		agent.run_playbook = run_playbook
		agent.ansible_variables = self.variables(engine_scrape_interval="7s")
		MonitoringAgent.write_targets(agent)

		self.assertEqual(played["playbook"], "push_targets.yml")
		self.assertEqual(played["extravars"]["monitoring_engine_scrape_interval"], "7s")
		# Still the play's own reason for existing — the lists have to come through too.
		self.assertIn("monitoring_host_targets", played["extravars"])

	def test_a_blank_field_leaves_the_role_default_standing(self):
		config = self.scrape_config()
		[engine] = [j for j in config["scrape_configs"] if j["job_name"] == "grove-engine"]
		self.assertEqual(
			engine["scrape_interval"], self.VMAGENT_DEFAULTS["monitoring_engine_scrape_interval"]
		)

	def test_a_blank_field_is_never_passed_as_an_empty_extra_var(self):
		# An extra-var beats a role default even when it is empty, so a blank passed through would
		# write `scrape_interval:` with nothing after it — a config vmagent refuses to start on.
		# This is why the fallbacks are omissions rather than defaults repeated in Python.
		variables = self.variables()
		for key in (
			"monitoring_scrape_interval",
			"monitoring_engine_scrape_interval",
			"vmagent_flush_interval",
			"vmagent_max_disk_usage",
		):
			with self.subTest(key):
				self.assertNotIn(key, variables)
				self.assertIn(key, self.VMAGENT_DEFAULTS)

	def test_the_unit_takes_the_docs_buffer_settings(self):
		unit = TEMPLATES.get_template("vmagent.service.j2").render(
			**{**self.VMAGENT_DEFAULTS, **self.variables(flush_interval="2s", max_disk_usage="8GiB")}
		)
		self.assertIn("-remoteWrite.flushInterval=2s", unit)
		self.assertIn("-remoteWrite.maxDiskUsagePerURL=8GiB", unit)


if __name__ == "__main__":
	unittest.main()
