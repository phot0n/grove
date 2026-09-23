# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Small helpers shared across the app. Nothing here reaches into a doctype; keep it that way."""

import os
import re

import frappe

MIB_PER_GB = 1024

DNS_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def vram_gb_from_mib(mib):
	"""MiB → whole marketed GB. Rounds half up, unlike Python's banker's round(): an L4 reporting
	exactly 23040 MiB is a 24 GB card, and round() would call it 22."""
	return int(mib // MIB_PER_GB + (1 if mib % MIB_PER_GB * 2 >= MIB_PER_GB else 0))


def playbooks_root():
	"""Path to grove/playbooks — every ansible project this app ships."""
	return os.path.join(frappe.get_app_path("grove"), "playbooks")


def ansible_project_dir(doctype):
	"""'Inference Server' → .../playbooks/inference_server. Playbooks at the top, roles in roles/
	beside them."""
	return os.path.join(playbooks_root(), doctype.lower().replace(" ", "_"))


def shared_roles_dir():
	"""Roles more than one doctype's playbooks use. Ansible searches it AFTER a playbook's own
	roles/, so a role is written once and named from anywhere."""
	return os.path.join(playbooks_root(), "roles")


def is_env_key(name):
	"""Env rows are interpolated into a systemd unit and a `docker run` argv, so anything not
	POSIX-shaped is rejected before it gets there."""
	return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))


def is_env_value(value):
	"""Values render as Environment="KEY=<value>", so a newline starts a fresh directive and a
	double quote ends the assignment early."""
	return not re.search(r'[\n\r"]', value or "")


def is_id_safe(name):
	"""True when a doc name survives the gateway's request-id sanitiser without losing itself.

	`CleanIDPart` rewrites '-' to '_' so the only '-' left in an id is its own separator, and
	silently DROPS everything else. That is reversible only while the name carries no '_' of its
	own: `inf-a` and `inf_a` both arrive as `inf_a`, and `inf.a` arrives as `infa`."""
	return bool(re.fullmatch(r"[A-Za-z0-9-]+", name or ""))


def validate_id_safe_name(doctype, name):
	if not name or is_id_safe(name):
		return

	frappe.throw(
		f"{doctype} name '{name}' can only contain letters, digits and '-'. The gateway "
		f"rewrites '-' to '_' when it stamps a request id, so a name holding '_' or "
		f"punctuation cannot be read back out of one.",
		title="Name is not traceable",
	)


def is_dns_name(name):
	"""True for a bare DNS name — dot-separated labels and nothing else. What can go in an
	nginx server_name and a certificate subject, so a scheme, a port, a path or a trailing dot
	all fail here rather than at `openresty -t` on a box that is already live."""
	labels = (name or "").split(".")
	return bool(name) and len(name) <= 253 and all(DNS_LABEL.fullmatch(label) for label in labels)


def is_label_under(name, zone):
	"""True when `name` is exactly one label below `zone`. A wildcard certificate matches one
	label and no more: `*.grove.example.com` covers `api.grove.example.com`, but neither the
	apex nor `api.eu.grove.example.com`."""
	suffix = f".{zone}"
	return name.endswith(suffix) and "." not in name[: -len(suffix)]


def slugify(text):
	"""'Qwen3.5 Coder_Next' → 'qwen3.5-coder-next'. Lowercased; runs of whitespace,
	underscores and dashes collapse to one dash."""
	return re.sub(r"[\s_-]+", "-", (text or "").strip().lower()).strip("-")
