# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What the Deploy Agent button ships. Pure — the doc is a SimpleNamespace and run_playbook is
recorded, so no site and no SSH.

The play writes /etc/grove-gateway/agent.env whole, from extra-vars. That file holds the admin
token every /admin push authenticates with, so a caller that omits one variable does not leave a
stale value behind — it writes a blank one, and the agent refuses to start on a blank token.
These pin that the button passes everything the file renders.
"""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from grove.grove.doctype.gateway_server.gateway_server import GatewayServer

# Anchored to this file, not the cwd: `bench run-tests` runs from the bench root, where a path
# relative to the app does not resolve. Same shape the template tests use.
PLAY = Path(__file__).parent.parent.parent / "playbooks" / "gateway_server" / "deploy_agent.yml"
TTL = "2m"


def deploy_agent_extravars():
	"""Run _deploy_agent against a fake doc and return the extra-vars it passed."""
	sent = {}
	proxy = SimpleNamespace(
		name="proxy-1",
		region="ap-south-1",
		get_password=lambda field: f"secret-{field}",
		run_playbook=lambda play, extravars: sent.update({"play": play, **extravars}),
	)
	settings = SimpleNamespace(gateway_variables={"synthetic_session_ttl": TTL})
	with (
		patch("frappe.get_single", return_value=settings),
		patch(
			"grove.grove.doctype.gateway_server.gateway_server.gateway_service_source",
			return_value="/tmp/src",
		),
	):
		GatewayServer._deploy_agent(proxy)
	return sent


def agent_env_variables():
	"""The Jinja variables the play's agent.env content renders, read from the play itself."""
	with open(PLAY) as play:
		tasks = yaml.safe_load(play)[0]["tasks"]
	content = next(t["ansible.builtin.copy"]["content"] for t in tasks if t["name"] == "agent.env")
	return {name for name in re.findall(r"{{\s*(\w+)", content)}


class TestDeployAgentShipsItsEnvFile(unittest.TestCase):
	def test_it_runs_the_agent_play(self):
		self.assertEqual(deploy_agent_extravars()["play"], "deploy_agent.yml")

	def test_it_passes_every_variable_the_env_file_renders(self):
		"""The general form of the hazard: the next variable added to agent.env is written blank
		unless the button passes it too."""
		sent = deploy_agent_extravars()
		self.assertEqual(agent_env_variables() - set(sent), set())

	def test_the_admin_token_is_not_blank(self):
		"""A blank one is fatal to the agent (requireAdminToken), so the box would come back
		crash-looping instead of serving."""
		self.assertTrue(deploy_agent_extravars()["admin_token"])

	def test_the_tuning_comes_from_grove_settings(self):
		self.assertEqual(deploy_agent_extravars()["synthetic_session_ttl"], TTL)
