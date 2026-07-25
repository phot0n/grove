# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Server provisioning via Ansible (§2/§8A). Builds the Go agent, then runs the
role playbook against the server's Machine, logging to Ansible Play/Task."""

import os

import frappe


def _app_grove_root():
	# .../apps/grove (parent of the `grove` python package dir).
	return os.path.dirname(frappe.get_app_path("grove"))


def build_agent():
	"""Return path to gateway_service source (build happens on the proxy)."""
	return os.path.join(_app_grove_root(), "gateway_service")
