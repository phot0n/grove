# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Ansible playbook abstraction. Wraps ansible_runner with sensible defaults for
playbook/role paths and server connections, so callers provide playbook + server details
directly without path/doc juggling."""

import os

import frappe

from grove import ansible_runner
from grove.utils import ansible_project_dir, app_grove_root


class AnsibleHost:
	"""Mixin for a doc with a box behind it: every Machine, and every server doctype standing
	on one. Gives them all one way to run a playbook against that box — callers name a
	playbook, never a project path, a server type or a Machine."""

	@property
	def playbook_machine(self):
		"""The Machine a playbook from this doc runs against — the box this doc links. A doc
		that IS the box overrides this to name itself."""
		if not self.machine:
			frappe.throw(f"{self.doctype} {self.name} has no Machine to run a playbook against.")
		return self.machine

	def run_playbook(self, playbook, project=None, extravars=None, **kwargs):
		"""Run one playbook against this doc's box, tracked as an Ansible Play.

		The playbook comes from this doctype's own folder. `project` names another doctype's
		when the play is shared — exporters.yml belongs to Monitoring Agent but runs against
		Inference and Proxy Server boxes."""
		ansible = Ansible(project_root=ansible_project_dir(project or self.doctype))
		return ansible.run_playbook(
			playbook_name=playbook,
			server_type=self.doctype,
			server_name=self.name,
			machine_name=self.playbook_machine,
			extravars=extravars,
			**kwargs,
		)


class Ansible:
	"""Wrapper around ansible_runner. Knows the default project directory
	(playbooks/) and provides clean API for running playbooks.

	Two usage styles:

	1. Direct (ad-hoc): Ansible(playbook="site.yml", server="10.0.0.1", user="root", variables={...}).run()
	2. Doctype-integrated: Ansible(project_root=...).run_playbook(playbook_name=..., server_type=..., ...)
	"""

	def __init__(self, playbook=None, project_root=None, server=None, user=None, port=None, variables=None):
		"""Initialize Ansible. Two modes:

		Ad-hoc mode (direct server):
			Ansible(playbook="site.yml", server="10.0.0.1", user="root", variables={...})
			  → run() executes immediately

		Doctype-integrated mode (no immediate run):
			Ansible(project_root="/path/to/ansible")
			  → run_playbook(...) runs with Frappe-tracked Ansible Play/Task

		Args:
			playbook: Playbook name for ad-hoc mode
			project_root: Custom Ansible project root (defaults to playbooks/)
			server: Server IP/hostname for ad-hoc mode
			user: SSH user for ad-hoc mode (default: root)
			port: SSH port for ad-hoc mode (default: 22)
			variables: Dict of Ansible variables for ad-hoc mode
		"""
		self.playbook = playbook
		self.server = server
		self.user = user or "root"
		self.port = port or 22
		self.variables = variables or {}

		if project_root:
			self.project_root = project_root
		else:
			self.project_root = os.path.join(app_grove_root(), "playbooks")

	def run(self):
		"""Execute playbook in ad-hoc mode (direct server connection, no Frappe tracking).
		Requires self.playbook, self.server to be set."""
		if not self.playbook or not self.server:
			raise ValueError("Ad-hoc mode requires playbook= and server= arguments")

		import subprocess
		import json

		playbook_path = self._resolve_playbook_path(self.playbook)

		# Run via ansible-playbook directly (not ansible_runner, which tracks in Frappe)
		cmd = [
			"ansible-playbook",
			playbook_path,
			"-i", f"{self.server},",  # trailing comma = single host
			"-u", self.user,
			"-p", str(self.port),
		]
		if self.variables:
			cmd.extend(["-e", json.dumps(self.variables)])

		# Set ANSIBLE_ROLES_PATH in a copy of the environment; don't pollute global
		env = os.environ.copy()
		roles_path = os.path.join(self.project_root, "roles")
		if os.path.isdir(roles_path):
			env["ANSIBLE_ROLES_PATH"] = roles_path

		rc = subprocess.call(cmd, cwd=self.project_root, env=env)
		return rc

	def _resolve_playbook_path(self, playbook_name):
		"""Resolve playbook name to full path. Searches:
		1. playbooks/{name}
		2. {name}
		3. {name}.yml
		Returns the path if found, else raises error."""
		candidates = [
			os.path.join(self.project_root, "playbooks", playbook_name),
			os.path.join(self.project_root, playbook_name),
			os.path.join(self.project_root, f"{playbook_name}.yml") if not playbook_name.endswith(".yml") else None,
		]

		for path in candidates:
			if path and os.path.isfile(path):
				return path

		raise FileNotFoundError(
			f"Playbook '{playbook_name}' not found in {self.project_root}. "
			f"Searched: playbooks/{playbook_name}, {playbook_name}, {playbook_name}.yml"
		)


	def run_playbook(self, playbook_name, server_type, server_name, machine_name, extravars=None, skip_tags=None, reference_doctype=None, reference_docname=None):
		"""Run a playbook via ansible_runner with Frappe tracking (Ansible Play/Task docs).
		Doctype-integrated mode — requires server_type + server_name for Frappe tracking.

		Args:
			playbook_name: Name of the playbook (e.g. "bootstrap.yml", "inference-host")
			server_type: DocType of the server (Machine, Proxy Server, etc.)
			server_name: Name of the server doc
			machine_name: Name of the Machine doc (same as server_name for Machine, different for role servers)
			extravars: Optional dict of extra variables to pass to Ansible
			skip_tags: Optional list of tags to skip (e.g. ["heavy"])
			reference_doctype: Optional DocType that triggered this play (defaults to server_type)
			reference_docname: Optional doc name that triggered this play (defaults to server_name)

		Returns:
			(play_name, rc): Ansible Play doc name and return code (0 = success)
		"""
		playbook_path = self._resolve_playbook_path(playbook_name)

		kwargs = {
			"playbook": os.path.basename(playbook_path),
			"server_type": server_type,
			"server_name": server_name,
			"machine_name": machine_name,
			"project_dir": self.project_root,
			"extravars": extravars or {},
		}
		if skip_tags:
			kwargs["skip_tags"] = skip_tags
		if reference_doctype:
			kwargs["reference_doctype"] = reference_doctype
		if reference_docname:
			kwargs["reference_docname"] = reference_docname

		return ansible_runner.run_play(**kwargs)


# Singleton instance for module-level access
_ansible_instance = None


def get_ansible(project_root=None):
	"""Get or create the default Ansible instance."""
	global _ansible_instance
	if _ansible_instance is None:
		_ansible_instance = Ansible(project_root)
	return _ansible_instance
