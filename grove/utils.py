# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Small helpers shared across the app — paths into the checked-out app and string
slugging. Nothing here reaches into a doctype; keep it that way."""

import os
import re

import frappe


def app_grove_root():
	"""Path to .../apps/grove — the parent of the `grove` python package, i.e. the repo
	root where deploy/ and gateway_service/ live."""
	return os.path.dirname(frappe.get_app_path("grove"))


def ansible_project_dir(component):
	"""The ansible project directory for a deploy component, e.g. 'vllm' →
	.../apps/grove/deploy/vllm/ansible."""
	return os.path.join(app_grove_root(), "deploy", component, "ansible")


def gateway_service_source():
	"""Path to the gateway agent's Go source. Shipped to the proxy, which builds it there
	for its own architecture — nothing is compiled here."""
	return os.path.join(app_grove_root(), "gateway_service")


def slugify(text):
	"""'Qwen3.5 Coder_Next' → 'qwen3.5-coder-next'. Lowercased; runs of whitespace,
	underscores and dashes collapse to one dash."""
	return re.sub(r"[\s_-]+", "-", (text or "").strip().lower()).strip("-")
