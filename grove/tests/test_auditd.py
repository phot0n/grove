# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The auditd rules file. auditctl stops at the first line it rejects, and augenrules then leaves
the box with only the rules above it, so every line must be a rule the box's arch can load."""

import re
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = Path(__file__).parent.parent / "playbooks/roles/auditd/templates"
# Syscalls arm64 dropped for their *at forms: auditctl rejects the name outright there.
LEGACY = {"creat", "open", "chmod", "chown", "lchown", "unlink", "rename", "stime"}


def render(architecture):
	environment = Environment(
		loader=FileSystemLoader(TEMPLATES), trim_blocks=True, undefined=StrictUndefined
	)
	scan = {"stdout_lines": ["/usr/bin/su", "/usr/bin/sudo"]}
	return environment.get_template("grove.rules.j2").render(
		ansible_architecture=architecture, auditd_privileged_binaries=scan
	)


def syscalls(rules):
	return set(re.findall(r"-S (\w+)", rules))


class TestAuditRules(unittest.TestCase):
	def test_every_line_is_one_rule(self):
		for architecture in ("x86_64", "aarch64"):
			with self.subTest(architecture):
				lines = render(architecture).splitlines()
				for line in lines:
					self.assertRegex(line, r"^-[aw] \S")
					self.assertEqual(line.count(" -k "), 1, line)

	def test_arm64_gets_no_legacy_syscall_and_no_32_bit_table(self):
		rules = render("aarch64")
		self.assertFalse(syscalls(rules) & LEGACY)
		self.assertNotIn("arch=b32", rules)

	def test_x86_keeps_both_tables_and_the_legacy_syscalls(self):
		rules = render("x86_64")
		self.assertIn("arch=b32", rules)
		self.assertLessEqual(LEGACY, syscalls(rules))

	def test_each_scanned_binary_gets_a_rule(self):
		self.assertIn("-F path=/usr/bin/sudo -F perm=x", render("x86_64"))

	def test_no_watch_ends_in_a_slash(self):
		for line in render("x86_64").splitlines():
			if line.startswith("-w "):
				self.assertFalse(line.split()[1].endswith("/"), line)
