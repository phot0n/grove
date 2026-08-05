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
from urllib.parse import parse_qs, quote, urlparse

import frappe
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from grove import monitoring
from grove.grove.doctype.grove_settings.grove_settings import SD_TOKEN_PATTERN
from grove.monitoring import (
	DCGM_EXPORTER_PORT,
	NODE_EXPORTER_PORT,
	VMAGENT_PORT,
	build_host_targets,
	engine_entry,
)

ROLES = Path(__file__).parent.parent.parent / "deploy/monitoring/ansible/roles"
VMAGENT_ROLE = ROLES / "vmagent"

TEMPLATES = Environment(
	loader=FileSystemLoader(VMAGENT_ROLE / "templates"),
	trim_blocks=True,
	keep_trailing_newline=True,
)


def box(name, ip="10.0.0.5", machine="MACHINE-1", region="ap-south-1", has_gpu=False):
	return {"name": name, "ip": ip, "machine": machine, "region": region, "has_gpu": has_gpu}


class TestHostTargets(unittest.TestCase):
	def test_every_box_exports_node_metrics(self):
		[entry] = build_host_targets([box("PROXY-1")])
		self.assertEqual(entry["targets"], ["10.0.0.5:9100"])
		self.assertEqual(entry["labels"], {"machine": "MACHINE-1", "region": "ap-south-1", "server": "PROXY-1"})

	def test_a_gpu_box_also_exports_dcgm(self):
		entries = build_host_targets([box("INF-1", has_gpu=True)])
		self.assertEqual([e["targets"][0] for e in entries], ["10.0.0.5:9100", "10.0.0.5:9400"])

	def test_a_box_without_gpus_has_no_dcgm_target(self):
		# DCGM is only installed on GPU boxes — a target for it elsewhere could only be down.
		entries = build_host_targets([box("PROXY-1")])
		self.assertEqual(len(entries), 1)

	def test_a_box_with_no_address_is_skipped(self):
		# A Machine that has not been provisioned yet has no IP to scrape.
		self.assertEqual(build_host_targets([box("INF-1", ip="")]), [])


class TestEngineTargets(unittest.TestCase):
	LABELS = {
		"model": "qwen3-35b",
		"deployment": "MD-00007",
		"server": "INF-blackwell",
		"machine": "MACHINE-1",
		"region": "ap-south-1",
	}

	def test_an_engine_is_scraped_where_the_gateway_routes_it(self):
		entry = engine_entry("http://10.0.0.9:8081", self.LABELS)
		self.assertEqual(entry["targets"], ["10.0.0.9:8081"])
		self.assertEqual(entry["labels"]["engine"], "http://10.0.0.9:8081")
		self.assertEqual(entry["labels"]["deployment"], "MD-00007")

	def test_an_engine_without_a_url_is_not_a_target(self):
		# Mid-provision deployments and still-loading pods hold "" — the same filter
		# gateway_sync applies to routes.
		self.assertIsNone(engine_entry("", self.LABELS))
		self.assertIsNone(engine_entry(None, self.LABELS))

	def test_the_scheme_travels_with_the_target(self):
		entry = engine_entry("https://pod.example.net/", {"model": "m", "deployment": "POD-1"})
		self.assertEqual(entry["targets"], ["pod.example.net:443"])
		self.assertEqual(entry["labels"]["__scheme__"], "https")

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
		return yaml.safe_load((ROLES / role / "defaults/main.yml").read_text())[key]

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
		# Nothing else installs one there: the agent box carries no Inference/Proxy Server doc.
		play = yaml.safe_load((ROLES.parent / "agent.yml").read_text())[0]
		self.assertIn("node_exporter", play["roles"])


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


class TestTheAgentPlayChecksBeforeItInstalls(unittest.TestCase):
	"""agent.yml installs node_exporter before vmagent. A requirement checked inside the vmagent
	role therefore fails with node_exporter already on the box — a half-built machine and a play
	log to read it out of. The checks belong ahead of every role."""

	PLAY = yaml.safe_load((ROLES.parent / "agent.yml").read_text())[0]

	def test_every_setting_the_play_needs_is_asserted_before_any_role_runs(self):
		asserted = yaml.dump(self.PLAY["pre_tasks"])
		for variable in (
			"monitoring_remote_write_url",
			"monitoring_remote_write_token",
			"monitoring_sd_url",
			"monitoring_sd_token",
			"monitoring_agent",
		):
			with self.subTest(variable):
				self.assertIn(variable, asserted)

	def test_no_role_defers_a_check_until_after_something_is_installed(self):
		for role in self.PLAY["roles"]:
			with self.subTest(role):
				tasks = yaml.safe_load((ROLES / role / "tasks/main.yml").read_text())
				self.assertNotIn("ansible.builtin.assert", {key for task in tasks for key in task})


class TestEveryTemplateVariableHasADefault(unittest.TestCase):
	"""A template variable no role defaults is an AnsibleUndefinedVariable at run time — on the
	box, mid-play, after the earlier tasks already changed it. Rendering each role's templates
	against its own defaults alone catches it here instead: the extra-vars Grove passes only ever
	override names `defaults/main.yml` already declares."""

	def test_each_role_defaults_every_variable_its_templates_use(self):
		for role in sorted(path.name for path in ROLES.iterdir() if (path / "templates").is_dir()):
			defaults = yaml.safe_load((ROLES / role / "defaults/main.yml").read_text()) or {}
			environment = Environment(
				loader=FileSystemLoader(ROLES / role / "templates"), undefined=StrictUndefined
			)
			for template in environment.list_templates():
				with self.subTest(f"{role}/{template}"):
					environment.get_template(template).render(**defaults)


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
	}

	def setUp(self):
		self.config = yaml.safe_load(TEMPLATES.get_template("scrape.yml.j2").render(**self.BASE))
		self.jobs = {job["job_name"]: job for job in self.config["scrape_configs"]}

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
		tasks = yaml.safe_load((VMAGENT_ROLE / "tasks/main.yml").read_text())
		[write] = [task for task in tasks if task.get("ansible.builtin.template", {}).get("src") == "scrape.yml.j2"]
		self.assertEqual(write["ansible.builtin.template"]["mode"], "0600")


if __name__ == "__main__":
	unittest.main()
