# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Small helpers shared across the app — paths into the checked-out app and string
slugging. Nothing here reaches into a doctype; keep it that way."""

import os
import re

import frappe

MIB_PER_GB = 1024


def vram_gb_from_mib(mib):
	"""MiB → whole marketed GB. Rounds half up, unlike Python's round(), which is banker's:
	an L4 reporting exactly 23040 MiB is a 24 GB card, and round() would call it 22."""
	return int(mib // MIB_PER_GB + (1 if mib % MIB_PER_GB * 2 >= MIB_PER_GB else 0))


def app_grove_root():
	"""Path to .../apps/grove — the parent of the `grove` python package, i.e. the repo
	root where playbooks/ and gateway_service/ live."""
	return os.path.dirname(frappe.get_app_path("grove"))


def ansible_project_dir(doctype):
	"""The ansible project directory for the doctype whose boxes a playbook runs against, e.g.
	'Inference Server' → .../apps/grove/playbooks/inference_server. Its playbooks sit at
	the top and its roles in roles/ beside them; a role two doctypes share is symlinked in."""
	return os.path.join(
		app_grove_root(), "playbooks", doctype.lower().replace(" ", "_")
	)


def shared_roles_dir():
	"""playbooks/roles — roles more than one doctype's playbooks use (the exporters, which go
	on every box whoever owns it). Ansible searches it after a playbook's own roles/, so a role
	is written once and named from anywhere without a copy or a symlink to keep pointing."""
	return os.path.join(app_grove_root(), "playbooks", "roles")


def gateway_service_source():
	"""Path to the gateway agent's Go source. Shipped to the proxy, which builds it there
	for its own architecture — nothing is compiled here."""
	return os.path.join(app_grove_root(), "gateway_service")


def is_env_key(name):
	"""True for a POSIX-shaped env var name. Env rows are interpolated into a systemd unit
	and a `docker run` argv, so anything else is rejected before it gets there."""
	return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))


def is_env_value(value):
	"""True when the value survives a systemd unit intact. Values render as
	Environment="KEY=<value>", so a newline would start a fresh directive (ExecStart= and
	friends) and a double quote would end the assignment early."""
	return not re.search(r'[\n\r"]', value or "")


def slugify(text):
	"""'Qwen3.5 Coder_Next' → 'qwen3.5-coder-next'. Lowercased; runs of whitespace,
	underscores and dashes collapse to one dash."""
	return re.sub(r"[\s_-]+", "-", (text or "").strip().lower()).strip("-")
