# Copyright (c) 2026, Grove and contributors
# See license.txt
"""How a doc reaches its playbooks. Pure — the runner is stubbed, so nothing is queued and no
box is touched; what is asserted is the project folder, the Machine and the play's owner."""

import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import yaml

from grove import ansible
from grove.ansible import AnsibleHost

PLAYBOOKS = Path(__file__).parent.parent.parent / "playbooks"
SHARED_ROLES = PLAYBOOKS / "roles"


def named_roles(node):
	"""Every role a playbook names, however it names one: a `roles:` entry (a string or a
	{role: x, when: y} dict) or an include_role anywhere in its tasks."""
	if isinstance(node, dict):
		for key, value in node.items():
			if key == "roles":
				for entry in value:
					yield entry["role"] if isinstance(entry, dict) else entry
			elif key.endswith("include_role"):
				yield value["name"]
			else:
				yield from named_roles(value)
	elif isinstance(node, list):
		for entry in node:
			yield from named_roles(entry)


def host(doctype, name, machine):
	"""Stand-in for a doc using the mixin, with its Machine already resolved."""
	return SimpleNamespace(doctype=doctype, name=name, playbook_machine=machine)


class TestPlaybookMachine(unittest.TestCase):
	def test_a_server_doc_names_the_box_it_links(self):
		server = SimpleNamespace(doctype="Inference Server", name="INF-1", machine="MACHINE-1")
		self.assertEqual(AnsibleHost.playbook_machine.fget(server), "MACHINE-1")

	def test_a_server_with_no_box_is_refused_rather_than_guessed_at(self):
		# Without this it would fall through to the server's own name and look up a Machine
		# that does not exist — or worse, one that happens to share the name.
		server = SimpleNamespace(doctype="Inference Server", name="INF-1", machine=None)
		with self.assertRaises(Exception):
			AnsibleHost.playbook_machine.fget(server)


class TestRunPlaybook(unittest.TestCase):
	def run_playbook(self, doc, playbook, **kwargs):
		"""The kwargs the runner would have been called with. The playbook path is resolved for
		real on the way through, so a moved folder fails here."""
		with unittest.mock.patch.object(ansible.ansible_runner, "run_play") as run_play:
			AnsibleHost.run_playbook(doc, playbook, **kwargs)
		return run_play.call_args.kwargs

	def test_a_playbook_comes_from_its_own_doctypes_folder(self):
		called = self.run_playbook(host("Machine", "MACHINE-1", "MACHINE-1"), "scan_gpus.yml")
		self.assertTrue(called["project_dir"].endswith("/playbooks/machine"))
		self.assertEqual(called["playbook"], "scan_gpus.yml")

	def test_the_play_is_owned_by_the_doc_and_run_against_its_box(self):
		called = self.run_playbook(host("Inference Server", "INF-1", "MACHINE-1"), "serve.yml")
		self.assertEqual(called["server_type"], "Inference Server")
		self.assertEqual(called["server_name"], "INF-1")
		self.assertEqual(called["machine_name"], "MACHINE-1")

	def test_a_shared_play_is_taken_from_the_doctype_that_owns_it(self):
		# exporters.yml is a Monitoring Agent play; it runs against Inference and Gateway Server
		# boxes, which is what `project` is for.
		called = self.run_playbook(
			host("Gateway Server", "PROXY-1", "MACHINE-2"), "exporters.yml", project="Monitoring Agent"
		)
		self.assertTrue(called["project_dir"].endswith("/playbooks/monitoring_agent"))
		self.assertEqual(called["server_type"], "Gateway Server", "the play still belongs to the box")

	def test_a_playbook_that_is_not_there_fails_before_anything_is_queued(self):
		with self.assertRaises(FileNotFoundError):
			self.run_playbook(host("Machine", "MACHINE-1", "MACHINE-1"), "nonexistent.yml")


class TestEveryRoleAPlaybookNamesResolves(unittest.TestCase):
	"""Ansible looks in the playbook's own roles/ dir, then in playbooks/roles. A role in
	neither is an error raised on the box, mid-play, after earlier tasks already changed it —
	so the same two places are checked here, where a moved or renamed role costs nothing."""

	def playbooks(self):
		return sorted(p for p in PLAYBOOKS.glob("*/*.yml"))

	def test_a_playbook_is_found_for_every_doctype_folder(self):
		# The walk below proves nothing if the glob quietly matched nothing.
		folders = {path.parent.name for path in self.playbooks()}
		self.assertEqual(
			folders, {"machine", "inference_server", "gateway_server", "monitoring_agent"}
		)

	def test_every_role_resolves_in_its_own_folder_or_the_shared_one(self):
		for playbook in self.playbooks():
			own_roles = playbook.parent / "roles"
			for role in named_roles(yaml.safe_load(playbook.read_text())):
				with self.subTest(f"{playbook.parent.name}/{playbook.name}: {role}"):
					self.assertTrue(
						(own_roles / role).is_dir() or (SHARED_ROLES / role).is_dir(),
						f"role '{role}' is in neither {own_roles} nor {SHARED_ROLES}",
					)

	def test_a_shared_role_is_not_also_copied_into_a_doctype_folder(self):
		# Two copies drift, and which one runs would depend on the playbook that named it.
		shared = {path.name for path in SHARED_ROLES.iterdir() if path.is_dir()}
		for roles_dir in PLAYBOOKS.glob("*/roles"):
			if roles_dir == SHARED_ROLES:
				continue
			for role in roles_dir.iterdir():
				with self.subTest(f"{roles_dir.parent.name}/{role.name}"):
					self.assertNotIn(role.name, shared)


if __name__ == "__main__":
	unittest.main()
