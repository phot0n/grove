# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Machine Type is a Select of doctype names, and the Machine form builds a Create/Open button
out of whichever one is set. Nothing checks that at runtime: a value that is not a doctype with
a `machine` Link opens an empty form or silently finds nothing, so it is checked here.
"""

import json
import unittest
from pathlib import Path

DOCTYPES = Path(__file__).parent.parent.parent / "grove/grove/doctype"


def doctype_json(name):
	slug = name.lower().replace(" ", "_")
	return json.loads((DOCTYPES / slug / f"{slug}.json").read_text())


class TestMachineTypesAreCreatableDoctypes(unittest.TestCase):
	def setUp(self):
		options = next(
			field["options"]
			for field in doctype_json("Machine")["fields"]
			if field["fieldname"] == "machine_type"
		)
		self.machine_types = [option for option in options.split("\n") if option]

	def test_the_monitoring_agent_is_one_of_them(self):
		# It is Machine-backed like the servers are, so it is created the same way.
		self.assertIn("Monitoring Agent", self.machine_types)

	def test_every_type_names_a_doctype_that_links_back_to_a_machine(self):
		# The form prefills {machine: <this box>} on create and looks the doc up by it after.
		for machine_type in self.machine_types:
			with self.subTest(machine_type):
				[field] = [
					field
					for field in doctype_json(machine_type)["fields"]
					if field["fieldname"] == "machine"
				]
				self.assertEqual(field["options"], "Machine")


if __name__ == "__main__":
	unittest.main()
