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


PLAYBOOKS = Path(__file__).parent.parent / "playbooks"
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
def resolve(variables):
	"""A default that references another default (grove_tls_cert → grove_tls_dir) is resolved lazily
	by Ansible and not at all by plain Jinja. Without this the config renders with the braces still
	in it and every path assertion passes against a string nginx could not use.

	Lived in the gateway template tests until those went with the nginx they covered. The inference
	box still runs one, so this came along."""
	plain = Environment()
	for _ in range(2):
		variables = {
			key: plain.from_string(value).render(**variables)
			if isinstance(value, str) and "{{" in value
			else value
			for key, value in variables.items()
		}
	return variables


PROXY_VARS = resolve({
	**role_defaults(PLAYBOOKS / "roles/grove_https"),
	**role_defaults(PLAYBOOKS / "roles/openresty"),
	**role_defaults(INFERENCE_ROLES / "engine_proxy"),
})

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
	"vllm_api_key": "deadbeef",
	"vllm_engine_kind": "vllm",
	"vllm_health_path": "/health",
	# Role defaults: the play pre-downloads the whole repo, so nothing is a GGUF file glob.
	"vllm_predownload_model": True,
	"vllm_download_glob": "",
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

	def test_the_image_is_invoked_exactly_as_the_fleet_already_invokes_it(self):
		# The byte-identity guard for the engine-class split. Every running deployment holds this
		# line in /opt/vllm/containers/vllm-<slug>.sh; re-rendering it differently — a changed
		# quoting style, a moved argument — notifies `recreate vllm container` and replaces every
		# engine in the fleet. The positional is unquoted and every flag is single-quoted, which is
		# also what stops an operator's argument reaching the shell.
		self.assertIn(
			"  vllm/vllm-openai:latest Qwen/Qwen3-35B '--port' '8081' '--tensor-parallel-size' '2'",
			render("vllm-container-run.sh.j2", BASE),
		)

	def test_a_custom_image_runs_its_own_entrypoint(self):
		# No positional and no flags of ours: the line ends at the image, and what the container
		# does next is the image's business. Still has to parse as shell.
		custom = {**BASE, "vllm_model": "", "vllm_serve_args": []}
		script = render("vllm-container-run.sh.j2", custom)
		self.assertIn("  vllm/vllm-openai:latest\n", script)
		check = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
		self.assertEqual(check.returncode, 0, check.stderr)

	def test_a_custom_startup_command_rides_the_entrypoint(self):
		# Each argument single-quoted, which is also what stops an operator's Startup Command
		# reaching the shell that runs this script.
		custom = {**BASE, "vllm_model": "", "vllm_serve_args": ["--http-port", "9000", "a b;c"]}
		self.assertIn(
			"  vllm/vllm-openai:latest '--http-port' '9000' 'a b;c'",
			render("vllm-container-run.sh.j2", custom),
		)

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

	def test_the_compile_cache_survives_a_container_replace(self):
		# Without this mount every replace re-runs torch.compile (~1 min of Loading).
		script = render("vllm-container-run.sh.j2", BASE)
		self.assertIn("-v /opt/vllm/cache:/root/.cache/vllm", script)


CACHE_LINES = [
	"TRITON_CACHE_DIR=/root/.cache/vllm/triton",
	"TORCHINDUCTOR_CACHE_DIR=/root/.cache/vllm/torchinductor",
]
FIXED_LINES = CACHE_LINES + ["HF_HUB_OFFLINE=1"]


class TestContainerEnvFile(unittest.TestCase):
	def test_one_key_value_per_line(self):
		lines = render("vllm-container.env.j2", BASE).splitlines()
		self.assertEqual(
			lines, FIXED_LINES + ["HF_TOKEN=hf_secret", "ODD=a b;c", "VLLM_API_KEY=deadbeef"]
		)

	def test_a_deployment_env_row_overrides_the_cache_defaults(self):
		# docker --env-file: the last occurrence of a key wins, so ours must come first.
		override = {**BASE, "vllm_env": {"TRITON_CACHE_DIR": "/elsewhere"}}
		lines = render("vllm-container.env.j2", override).splitlines()
		self.assertLess(lines.index(CACHE_LINES[0]), lines.index("TRITON_CACHE_DIR=/elsewhere"))

	def test_no_api_key_line_when_none_resolved(self):
		self.assertEqual(render("vllm-container.env.j2", BARE).splitlines(), FIXED_LINES)


class TestTheEngineDoesNotFetchWeightsItself(unittest.TestCase):
	"""The pre-download task owns the weights. Left online, the engine resolves `main` and
	pulls a new upstream revision inline — a deploy that changed nothing then serves
	different weights, or OOMs loading them."""

	def env(self, **overrides):
		return render("vllm-container.env.j2", {**BASE, **overrides}).splitlines()

	def test_a_predownloaded_repo_runs_offline(self):
		self.assertIn("HF_HUB_OFFLINE=1", self.env())

	def test_a_gguf_ref_stays_online(self):
		# Its download is one file; the config and tokenizer beside it are fetched at boot.
		self.assertNotIn("HF_HUB_OFFLINE=1", self.env(vllm_download_glob="*Q4_K_M.gguf"))

	def test_a_streaming_placement_stays_online(self):
		# Weights come from S3, but the tokenizer is still the hub's.
		self.assertNotIn("HF_HUB_OFFLINE=1", self.env(vllm_predownload_model=False))

	def test_it_can_be_overridden_per_deployment(self):
		lines = self.env(vllm_env={"HF_HUB_OFFLINE": "0"})
		self.assertLess(lines.index("HF_HUB_OFFLINE=1"), lines.index("HF_HUB_OFFLINE=0"))


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

	def test_a_vllm_engine_is_not_gated_at_the_proxy(self):
		# vLLM enforces VLLM_API_KEY itself, so a second check here would be a second place to
		# get wrong — and this file would then have to carry the key for every deployment.
		self.assertNotIn("$http_authorization", self.fragment)
		self.assertNotIn("deadbeef", self.fragment)

	def test_a_custom_engine_is_gated_at_the_proxy(self):
		# The bug this exists for: an image that serves itself enforces nothing, and this proxy is
		# the only thing between it and the box's 443. Without this the engine answers anyone who
		# can reach the box.
		fragment = render("engine-location.conf.j2", {**BASE, "vllm_engine_kind": "custom"})
		self.assertIn('if ($http_authorization != "Bearer deadbeef") { return 401; }', fragment)

	def test_a_custom_engine_with_no_key_refuses_everything(self):
		# Fails closed: a blank key must not render a check that anything satisfies.
		fragment = render(
			"engine-location.conf.j2", {**BASE, "vllm_engine_kind": "custom", "vllm_api_key": ""}
		)
		self.assertIn('if ($http_authorization != "Bearer ") { return 401; }', fragment)

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


class TestReconfigureRunsTheConfigTasksOnly(unittest.TestCase):
	"""Update Engine Config has its own play. It exists to be fast enough to run against a deploy
	that is still on its health gate, so anything that downloads, pulls or checks disk belongs in
	main.yml — never in the shared config subset."""

	def test_the_config_subset_is_imported_by_the_full_serve_not_copied(self):
		main = (INFERENCE_ROLES / "vllm/tasks/main.yml").read_text()
		self.assertIn("config.yml", main)

	def test_nothing_heavy_reaches_the_config_subset(self):
		config = (INFERENCE_ROLES / "vllm/tasks/config.yml").read_text()
		for absent in ("docker pull", "hf download", "df --output", "docker image inspect"):
			with self.subTest(absent):
				self.assertNotIn(absent, config)

	def test_the_play_runs_that_subset_and_no_roles_around_it(self):
		[play] = yaml.safe_load((PLAYBOOKS / "inference_server/reconfigure.yml").read_text())
		self.assertFalse(play.get("roles"))
		[task] = play["tasks"]
		self.assertEqual(
			task["ansible.builtin.import_role"], {"name": "vllm", "tasks_from": "config.yml"}
		)


class TestCompileCachePrewarmHooks(unittest.TestCase):
	"""With a weights bucket configured, the run script pulls the compile cache before the
	container and pushes it (backgrounded, after /health) once compiled. No bucket → the
	script renders exactly as before."""

	WITH_BUCKET = {
		**BASE,
		"vllm_cache_bucket": "s3://grove-weights",
		"vllm_cache_sync_script": "/opt/vllm/containers/vllm-md-00007-cache-sync.sh",
		"vllm_cache_sync_env": {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s3secret"},
		"vllm_tensor_parallel_size": 2,
		"vllm_model_slug": "Qwen--Qwen3-35B",
	}

	def test_no_bucket_renders_no_hooks(self):
		script = render("vllm-container-run.sh.j2", BASE)
		self.assertNotIn("cache-sync", script)

	def test_hooks_wrap_the_container(self):
		script = render("vllm-container-run.sh.j2", self.WITH_BUCKET)
		self.assertLess(script.index("cache-sync.sh pull"), script.index("docker run"))
		self.assertIn("nohup /opt/vllm/containers/vllm-md-00007-cache-sync.sh push", script)
		check = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
		self.assertEqual(check.returncode, 0, check.stderr)

	def test_sync_script_is_valid_shell_and_keys_on_every_invalidation_axis(self):
		sync = render("vllm-cache-sync.sh.j2", self.WITH_BUCKET)
		check = subprocess.run(["sh", "-n"], input=sync, text=True, capture_output=True)
		self.assertEqual(check.returncode, 0, check.stderr)
		# digest (torch/vLLM), GPU from nvidia-smi, TP, model — a :latest tag pins nothing.
		self.assertIn("RepoDigests", sync)
		self.assertIn("nvidia-smi", sync)
		self.assertIn("tp2/Qwen--Qwen3-35B", sync)

	def test_bucket_credentials_stay_in_the_sync_script(self):
		self.assertIn(
			"export AWS_SECRET_ACCESS_KEY='s3secret'",
			render("vllm-cache-sync.sh.j2", self.WITH_BUCKET),
		)
		self.assertNotIn("s3secret", render("vllm-container-run.sh.j2", self.WITH_BUCKET))


class TestPullAndDownloadOverlap(unittest.TestCase):
	"""A fresh box pays max(pull, weights), not the sum: the pull fires with poll: 0, the weight
	download runs while it streams, and an async_status collects it — carrying the recreate
	notify, since a poll: 0 register is only a job handle."""

	@classmethod
	def setUpClass(cls):
		cls.tasks = yaml.safe_load((INFERENCE_ROLES / "vllm/tasks/main.yml").read_text())

	def index_of(self, marker):
		return next(i for i, task in enumerate(self.tasks) if marker in str(task))

	def test_the_pull_is_fired_and_forgotten(self):
		pull = self.tasks[self.index_of("docker pull")]
		self.assertEqual(pull["poll"], 0)
		self.assertNotIn("notify", pull)

	def test_the_download_runs_between_fire_and_collect(self):
		pull, download = self.index_of("docker pull"), self.index_of("hf download")
		collect = self.index_of("ansible.builtin.async_status")
		self.assertLess(pull, download)
		self.assertLess(download, collect)

	def test_the_collect_owns_the_recreate(self):
		collect = self.tasks[self.index_of("ansible.builtin.async_status")]
		self.assertEqual(collect["notify"], "recreate vllm container")
		self.assertIn("Image is up to date", collect["changed_when"])


class TestWeightsOffTheDataVolume(unittest.TestCase):
	"""vllm_hf_home outside vllm_home — the instance-store opt-in. The weights then stop
	counting against the data volume, and their own mount is checked instead."""

	@classmethod
	def setUpClass(cls):
		cls.tasks = yaml.safe_load((INFERENCE_ROLES / "vllm/tasks/main.yml").read_text())

	def task(self, name):
		return next(t for t in self.tasks if t.get("name") == name)

	def on_data(self, vllm_home, vllm_hf_home):
		expression = self.task("Note whether the weights share the data volume")[
			"ansible.builtin.set_fact"
		]["vllm_weights_on_data"]
		rendered = Environment().from_string(expression).render(
			vllm_home=vllm_home, vllm_hf_home=vllm_hf_home
		)
		return rendered == "True"

	def test_the_default_layout_shares_the_volume(self):
		self.assertTrue(self.on_data("/opt/vllm", "/opt/vllm/hf"))

	def test_the_instance_store_layout_does_not(self):
		self.assertFalse(self.on_data("/opt/vllm", "/mnt/instance/hf"))
		# /opt/vllm-other must not read as "under /opt/vllm".
		self.assertFalse(self.on_data("/opt/vllm", "/opt/vllm-other/hf"))

	def test_off_volume_weights_leave_the_data_path_check(self):
		check = self.task("Check the data path can hold what is still to be downloaded")
		self.assertIn("vllm_weights_on_data", check["vars"]["weights_gb"])

	def test_the_separate_mount_check_only_runs_off_volume(self):
		# And never for a streaming deploy, which downloads nothing into the HF cache.
		block = self.task("Check the separate weights mount")
		self.assertEqual(
			block["when"], "(vllm_predownload_model | bool) and not (vllm_weights_on_data | bool)"
		)
		self.assertTrue(any("findmnt" in str(t) for t in block["block"]))

	def test_a_streaming_deploy_counts_no_weights_against_the_disk(self):
		check = self.task("Check the data path can hold what is still to be downloaded")
		self.assertIn("vllm_predownload_model", check["vars"]["weights_gb"])


class TestCertificateLifetime(unittest.TestCase):
	def test_a_box_certificate_is_short_lived(self):
		# Nothing renews it and nothing verifies it, so this number is only ever read by a human
		# deciding whether the fleet needs reissuing. Worth pinning so it is not quietly bumped.
		self.assertEqual(role_defaults(PLAYBOOKS / "roles/grove_https")["grove_tls_days"], 90)


class TestOneWebServerForTheFleet(unittest.TestCase):
	"""Every inference box's engine proxy runs the same pinned build. Two versions on one request
	path means two sets of defaults and two bug surfaces.

	It used to cover the gateway too. The gateway serves its own TLS now and installs no web server
	at all, so what is left to keep aligned is the inference fleet — and the check that the gateway
	stays OUT of it lives in test_gateway_config.py."""

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
		# It used to be a var inside a play's own tasks. Extracting the role is what stops one
		# inference box drifting to a different build than the next.
		pinned = role_defaults(PLAYBOOKS / "roles/openresty")["openresty_version"]
		self.assertRegex(pinned, r"^\d+\.\d+\.\d+\.\d+$")
		for play in ("inference_server/serve.yml", "inference_server/provision.yml"):
			with self.subTest(play):
				self.assertNotIn("openresty_version", (PLAYBOOKS / play).read_text())

	def test_every_inference_play_installs_it(self):
		plays = self.plays_using("openresty")
		self.assertIn("serve.yml", plays)
		self.assertIn("provision.yml", plays)

	def test_no_front_box_installs_it_any_more(self):
		# pathway owns :80 and :443 on a Gateway or Ingress Server. A play that reinstalled
		# OpenResty there would take the ports back from it on the next provision.
		plays = self.plays_using("openresty")
		self.assertNotIn("gateway.yml", plays)
		self.assertNotIn("ingress.yml", plays)

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


class TestWarmupGate(unittest.TestCase):
	"""A 200 from the health path means the container bound its port, nothing more. The warmup task is
	the first thing that asks the GPU for a forward pass, so it is what stands between a kernel
	that cannot run on this card and a gateway route pointing at it."""

	@classmethod
	def setUpClass(cls):
		cls.tasks = yaml.safe_load((INFERENCE_ROLES / "vllm/tasks/config.yml").read_text())
		cls.defaults = role_defaults(INFERENCE_ROLES / "vllm")

	def index_of(self, marker):
		return next(i for i, task in enumerate(self.tasks) if marker in str(task))

	def warmup(self):
		return self.tasks[self.index_of("vllm_warmup_request.path")]

	def test_it_runs_after_the_health_gate(self):
		# Before it, the engine has not bound its port and every attempt is a connection refused.
		self.assertLess(self.index_of("vllm_health_path"), self.index_of("vllm_warmup_request.path"))

	def test_it_carries_the_bearer(self):
		# The engine runs with VLLM_API_KEY set, so an unauthenticated POST is a 401 on a healthy
		# engine — a warmup that fails every deploy for the wrong reason.
		headers = self.warmup()["ansible.builtin.uri"]["headers"]
		self.assertEqual(headers["Authorization"], "Bearer {{ vllm_effective_api_key }}")

	def test_it_asks_the_engine_not_the_proxy(self):
		# Box-local plain HTTP. The nginx front carries a self-signed cert, and the assertion is
		# about the engine rather than the proxy in front of it.
		self.assertIn("http://127.0.0.1:{{ vllm_port }}", self.warmup()["ansible.builtin.uri"]["url"])

	def test_a_failed_warmup_fails_the_play(self):
		# rc != 0 is the whole mechanism: it leaves the deployment Broken and unpublished. Suppress
		# the failure and the route goes live on an engine that cannot serve a request.
		warmup = self.warmup()
		self.assertEqual(warmup["ansible.builtin.uri"]["status_code"], 200)
		for suppressor in ("failed_when", "ignore_errors"):
			self.assertNotIn(suppressor, warmup, suppressor)

	def test_it_is_on_by_default_and_switchable(self):
		self.assertIs(self.defaults["vllm_warmup"], True)
		self.assertEqual(self.defaults["vllm_warmup_request"], {})
		self.assertIn("vllm_warmup", self.warmup()["when"])

	def test_a_modality_with_nothing_to_prove_skips_it(self):
		# ServeCommand hands back {} for audio; the same guard covers a role run that sets nothing.
		self.assertIn("vllm_warmup_request | length > 0", self.warmup()["when"])
