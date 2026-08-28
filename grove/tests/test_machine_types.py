# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Machine Type reads as a role — `Inference`, not `Inference Server` — and the Machine form builds
a Create/Open button out of the doctype it maps to. Nothing checks that mapping at runtime: a value
it does not carry offers no button at all, and one naming a doctype with no `machine` Link opens an
empty form or silently finds nothing, so it is checked here.
"""

import json
import re
import unittest
from pathlib import Path

DOCTYPES = Path(__file__).parent.parent.parent / "grove/grove/doctype"


def doctype_json(name):
	slug = name.lower().replace(" ", "_")
	return json.loads((DOCTYPES / slug / f"{slug}.json").read_text())


def form_script_map():
	"""The `SERVER_DOCTYPE` literal the Machine form routes by.

	Read out of the script rather than restated here: a copy in this file would agree with itself
	while the form drifted, which is the one thing this is meant to catch."""
	script = (DOCTYPES / "machine" / "machine.js").read_text()
	literal = re.search(r"const SERVER_DOCTYPE = \{(.*?)\n\};", script, re.S).group(1)
	return dict(re.findall(r"'([^']+)':\s*'([^']+)'", literal))


class TestMachineTypesAreCreatableDoctypes(unittest.TestCase):
	def setUp(self):
		options = next(
			field["options"]
			for field in doctype_json("Machine")["fields"]
			if field["fieldname"] == "machine_type"
		)
		self.machine_types = [option for option in options.split("\n") if option]
		self.server_doctypes = form_script_map()

	def test_the_monitoring_agent_is_one_of_them(self):
		# It is Machine-backed like the servers are, so it is created the same way — and it is the
		# one type whose doctype name is the option, which is why the map is a map and not a suffix.
		self.assertIn("Monitoring Agent", self.machine_types)
		self.assertEqual(self.server_doctypes["Monitoring Agent"], "Monitoring Agent")

	def test_every_type_is_routed_by_the_form(self):
		# A type missing from the map is silent: the button simply never appears.
		self.assertEqual(sorted(self.server_doctypes), sorted(self.machine_types))

	def test_every_type_names_a_doctype_that_links_back_to_a_machine(self):
		# The form prefills {machine: <this box>} on create and looks the doc up by it after.
		for machine_type, server_doctype in self.server_doctypes.items():
			with self.subTest(machine_type):
				[field] = [
					field
					for field in doctype_json(server_doctype)["fields"]
					if field["fieldname"] == "machine"
				]
				self.assertEqual(field["options"], "Machine")


if __name__ == "__main__":
	unittest.main()
