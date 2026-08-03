# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The vllm role's engine templates. Pure — renders the files, no site and no box.

Both are parsed strictly by something else: the run script by /bin/sh, its env file by
docker. A stray blank line after a `\\` continuation or an unquoted value is a broken
deploy, not a failed assertion, so the parsers themselves are the assertion here.
"""

import subprocess
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Ansible renders with trim_blocks on; matching it is the whole point of this file.
TEMPLATES = Environment(
	loader=FileSystemLoader(
		Path(__file__).parent.parent.parent / "deploy/vllm/ansible/roles/vllm/templates"
	),
	trim_blocks=True,
	keep_trailing_newline=True,
)

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


if __name__ == "__main__":
	unittest.main()
