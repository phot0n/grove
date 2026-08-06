# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What an inference box is handed: the vllm role's engine templates, and the nginx front that
publishes them. Pure — renders the files, no site and no box.

Each is parsed strictly by something else: the run script by /bin/sh, its env file by docker, the
nginx files by nginx. A stray blank line after a `\\` continuation or an unquoted value is a broken
deploy, not a failed assertion, so the parsers themselves are the assertion where one is reachable.
"""

import subprocess
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

PLAYBOOKS = Path(__file__).parent.parent.parent / "playbooks"
INFERENCE_ROLES = PLAYBOOKS / "inference_server/roles"


def environment(templates_dir):
	# Ansible renders with trim_blocks on; matching it is the whole point of this file.
	return Environment(
		loader=FileSystemLoader(templates_dir), trim_blocks=True, keep_trailing_newline=True
	)


def role_defaults(path):
	return yaml.safe_load((path / "defaults/main.yml").read_text()) or {}


TEMPLATES = environment(INFERENCE_ROLES / "vllm/templates")
PROXY_TEMPLATES = environment(INFERENCE_ROLES / "engine_proxy/templates")

# engine_proxy names the certificate and htpasswd paths that grove_https owns, and grove_https runs
# ahead of it in every play that uses either — so its defaults are in scope on the box, and here.
PROXY_VARS = {
	**role_defaults(PLAYBOOKS / "roles/grove_https"),
	**role_defaults(PLAYBOOKS / "roles/openresty"),
	**role_defaults(INFERENCE_ROLES / "engine_proxy"),
}

BASE = {
	"vllm_unit": "vllm-md-00007",
	"vllm_instance": "md-00007",
	"vllm_port": 8081,
	"vllm_model": "Qwen/Qwen3-35B",
	"vllm_served_name": "qwen3-35b",
	"vllm_image": "vllm/vllm-openai:latest",
	"vllm_user": "root",
	"vllm_home": "/opt/vllm",
	"vllm_hf_home": "/opt/vllm/hf",
	"vllm_cache_dir": "/opt/vllm/cache",
	"vllm_container_env_file": "/opt/vllm/containers/vllm-md-00007.env",
	"vllm_serve_args": ["--port", "8081", "--tensor-parallel-size", "2"],
	"vllm_cuda_visible_devices": "0,1",
	"vllm_env": {"HF_TOKEN": "hf_secret", "ODD": "a b;c"},
	"vllm_effective_api_key": "deadbeef",
}
# The unpinned single-GPU box with no operator env and a caller-supplied key.
BARE = {**BASE, "vllm_cuda_visible_devices": "", "vllm_env": {}, "vllm_effective_api_key": ""}


def render(name, variables):
	return TEMPLATES.get_template(name).render(**variables)


class TestContainerRunScript(unittest.TestCase):
	def test_is_valid_shell(self):
		for label, variables in (("pinned", BASE), ("bare", BARE)):
			script = render("vllm-container-run.sh.j2", variables)
			with self.subTest(label):
				self.assertNotIn("\\\n\n", script, "blank line after a line continuation")
				check = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
				self.assertEqual(check.returncode, 0, check.stderr)

	def test_docker_owns_the_restart(self):
		script = render("vllm-container-run.sh.j2", BASE)
		self.assertIn("--restart unless-stopped", script)
		# --rm is incompatible with a restart policy, and would delete the container
		# the policy is meant to restart.
		self.assertNotIn("--rm ", script)

	def test_gpu_pinning(self):
		self.assertIn("--gpus '\"device=0,1\"'", render("vllm-container-run.sh.j2", BASE))
		self.assertIn("--gpus all", render("vllm-container-run.sh.j2", BARE))

	def test_secrets_stay_out_of_the_argv(self):
		# `ps` is world-readable; the env file is 0600.
		script = render("vllm-container-run.sh.j2", BASE)
		self.assertNotIn("hf_secret", script)
		self.assertNotIn("deadbeef", script)
		self.assertIn("--env-file /opt/vllm/containers/vllm-md-00007.env", script)


class TestContainerEnvFile(unittest.TestCase):
	def test_one_key_value_per_line(self):
		lines = render("vllm-container.env.j2", BASE).splitlines()
		self.assertEqual(lines, ["HF_TOKEN=hf_secret", "ODD=a b;c", "VLLM_API_KEY=deadbeef"])

	def test_no_api_key_line_when_none_resolved(self):
		self.assertEqual(render("vllm-container.env.j2", BARE), "")


class TestEngineProxyLocation(unittest.TestCase):
	"""The nginx fragment that puts this instance on the box's one public port. Included inside
	the engine proxy's server block, so it must be a bare location and nothing else."""

	def setUp(self):
		self.fragment = render("engine-location.conf.j2", BASE)

	def test_the_instance_answers_under_its_own_prefix(self):
		self.assertIn("location /e/md-00007/ {", self.fragment)
		self.assertNotIn("server {", self.fragment)

	def test_the_prefix_is_stripped_before_the_engine_sees_it(self):
		# The trailing slash is the whole mechanism: without it nginx forwards
		# /e/md-00007/v1/chat/completions verbatim and vLLM 404s every request.
		self.assertIn("proxy_pass http://127.0.0.1:8081/;", self.fragment)

	def test_streaming_survives_the_extra_hop(self):
		# Buffered, an SSE response arrives in one blob at the end — a failure that passes every
		# smoke test and ruins the product.
		self.assertIn("proxy_buffering off;", self.fragment)
		self.assertIn("proxy_http_version 1.1;", self.fragment)
		self.assertIn("proxy_read_timeout 600s;", self.fragment)

	def test_no_secret_reaches_a_world_readable_file(self):
		# This one is 0644 (nginx reads it), unlike the 0600 env file. The engine's key travels
		# per request from the gateway and has no business being here.
		self.assertNotIn("deadbeef", self.fragment)
		self.assertNotIn("hf_secret", self.fragment)


class TestEngineProxyConfig(unittest.TestCase):
	"""The box's front: one TLS server carrying every engine and the exporters, and nothing else."""

	def setUp(self):
		self.config = PROXY_TEMPLATES.get_template("grove.conf.j2").render(**PROXY_VARS)

	def test_it_is_a_whole_config_not_a_conf_d_snippet(self):
		# It overwrites OpenResty's packaged nginx.conf, which ships neither conf.d nor
		# sites-enabled. Owning the whole file is what removed the distro default site and the
		# `default_server` collision it used to cause.
		for directive in ("events {", "http {", "worker_processes"):
			with self.subTest(directive):
				self.assertIn(directive, self.config)

	def test_no_lua_runs_on_an_inference_box(self):
		# Auth, metering and routing live on the gateway. This box only terminates TLS and
		# forwards to a local port; it runs OpenResty for one fleet-wide build, not for Lua.
		for directive in ("lua_package_path", "_by_lua", "content_by_lua_block"):
			with self.subTest(directive):
				self.assertNotIn(directive, self.config)

	def test_the_box_serves_one_thing_over_tls_and_nothing_in_the_clear(self):
		# The security group opens 22 and 443 only. A `listen 80` here would put a socket on a
		# port nothing can reach — and if the group were ever widened by hand, it would answer.
		# No listener refuses harder than any block that answers could.
		self.assertEqual(self.config.count("server {"), 1)
		self.assertNotIn("listen 80", self.config)
		self.assertIn("listen 443 ssl", self.config)

	def test_the_tls_server_carries_the_engines(self):
		self.assertIn("ssl_certificate", self.config)
		self.assertIn("include", self.config)

	def test_the_exporters_are_behind_basic_auth(self):
		# They answer anyone who reaches them, so the front is the only thing gating them.
		self.assertEqual(self.config.count("auth_basic_user_file"), 2)

	def test_a_body_larger_than_nginxs_default_is_not_refused(self):
		# nginx defaults to 1m; the gateway accepts and forwards 32m. Without this every larger
		# request 413s and looks like the engine refused it.
		self.assertIn("client_max_body_size 32m;", self.config)


class TestCertificateLifetime(unittest.TestCase):
	def test_a_box_certificate_is_short_lived(self):
		# Nothing renews it and nothing verifies it, so this number is only ever read by a human
		# deciding whether the fleet needs reissuing. Worth pinning so it is not quietly bumped.
		self.assertEqual(role_defaults(PLAYBOOKS / "roles/grove_https")["grove_tls_days"], 90)


class TestOneWebServerForTheFleet(unittest.TestCase):
	"""The gateway's data path and every inference box's engine proxy run the same pinned build.
	Two versions on one request path means two sets of defaults and two bug surfaces."""

	def plays_using(self, role):
		found = []
		for play_file in sorted(PLAYBOOKS.rglob("*.yml")):
			if play_file.parts[-2] == "tasks" or "defaults" in play_file.parts:
				continue
			docs = yaml.safe_load(play_file.read_text())
			for play in docs if isinstance(docs, list) else []:
				names = [r if isinstance(r, str) else r.get("role") for r in play.get("roles") or []]
				if role in names:
					found.append(play_file.name)
		return found

	def test_the_version_is_pinned_in_exactly_one_place(self):
		# It used to be a var inside proxy.yml's own tasks. Extracting the role is what stops an
		# inference box drifting to a different build than the gateway.
		pinned = role_defaults(PLAYBOOKS / "roles/openresty")["openresty_version"]
		self.assertRegex(pinned, r"^\d+\.\d+\.\d+\.\d+$")
		for play in ("proxy_server/proxy.yml", "inference_server/serve.yml"):
			with self.subTest(play):
				self.assertNotIn("openresty_version", (PLAYBOOKS / play).read_text())

	def test_both_sides_of_the_fleet_install_it(self):
		plays = self.plays_using("openresty")
		self.assertIn("proxy.yml", plays)
		self.assertIn("serve.yml", plays)
		self.assertIn("provision.yml", plays)

	def test_it_installs_before_anything_that_writes_its_config(self):
		# grove_https puts the certificate on disk and engine_proxy writes the config; both are
		# meaningless before the package exists, and a `listen 443 ssl` block with no certificate
		# stops OpenResty starting at all.
		for play in ("inference_server/serve.yml", "inference_server/provision.yml"):
			with self.subTest(play):
				[doc] = yaml.safe_load((PLAYBOOKS / play).read_text())
				names = [r if isinstance(r, str) else r.get("role") for r in doc["roles"]]
				self.assertLess(names.index("openresty"), names.index("grove_https"))
				self.assertLess(names.index("grove_https"), names.index("engine_proxy"))


if __name__ == "__main__":
	unittest.main()
