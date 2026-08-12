# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What a Agent Sync Row records about a push. Pure — no site, no network.

The row is readable by anyone who can open the Desk, and the payloads carry live credentials: the
engine's own key on a replica row, the ingress's data token on a gateway row, and the hash a
gateway looks a customer's key up by. So redaction is the feature, not a nicety around it.
"""

import json
import re
import unittest
from pathlib import Path

import grove.agent_sync
import grove.usage_pull
from grove.agent_sync import _redact

SECRET = "a05749bce74d4e162f98339d3889f3483052d39957a0f3fc"

ROW_DOCTYPE = Path(grove.agent_sync.__file__).parent / "grove/doctype/agent_sync_row/agent_sync_row.json"
ROW_FIELDS = {field["fieldname"] for field in json.loads(ROW_DOCTYPE.read_text())["fields"]}


class TestRedaction(unittest.TestCase):
	def test_an_engine_key_never_reaches_the_row(self):
		payload = {"routes": {"qwen3.5-4b": [{"engine_url": "https://10.0.18.19/e/md", "internal_key": SECRET}]}}
		self.assertNotIn(SECRET, json.dumps(_redact(payload)))

	def test_it_is_marked_not_dropped(self):
		# "***" says the key WAS sent. A missing field reads as a push that forgot it, which is a
		# different bug and the one you would go looking for.
		[route] = _redact({"routes": {"m": [{"internal_key": SECRET}]}})["routes"]["m"]
		self.assertEqual(route["internal_key"], "***")

	def test_every_credential_shaped_key_goes(self):
		payload = {
			"keys": [{"key_hash": "abc", "prefix": "k1", "user": "u1"}],
			"admin_token": "t", "data_token": "t", "api_secret": "s",
		}
		text = json.dumps(_redact(payload))
		for leaked in ("abc", '"t"', '"s"'):
			self.assertNotIn(leaked, text)

	def test_everything_else_survives_intact(self):
		# The point of keeping the payload at all: the shape has to still be readable.
		payload = {"routes": {"m": [{"engine_url": "https://10.0.18.19/e/md", "capacity": 1024,
		                             "healthy": True, "deployment": "MD-00011", "internal_key": SECRET}]}}
		[route] = _redact(payload)["routes"]["m"]
		self.assertEqual(route["engine_url"], "https://10.0.18.19/e/md")
		self.assertEqual(route["capacity"], 1024)
		self.assertIs(route["healthy"], True)
		self.assertEqual(route["deployment"], "MD-00011")

	def test_a_blank_secret_is_left_alone(self):
		# "" already tells you nothing, and marking it would read as a key that was sent.
		self.assertEqual(_redact({"internal_key": ""})["internal_key"], "")

	def test_nested_structures_are_walked(self):
		payload = {"a": [{"b": {"c": [{"internal_key": SECRET}]}}]}
		self.assertNotIn(SECRET, json.dumps(_redact(payload)))


class TestEveryRowNamesTheBoxItIsAbout(unittest.TestCase):
	"""Frappe drops a key that is not a field on the child doctype, so a writer left behind on an old
	fieldname is not an error — the row comes out blank and the log stops saying which box it is about.
	That is how the Usage pull kept writing `proxy` after the field became `server`."""

	def rows_written(self):
		for module in (grove.agent_sync, grove.usage_pull):
			source = Path(module.__file__).read_text()
			for literal in re.findall(r'append\("results",\s*\{([^}]*)\}', source):
				yield module.__name__, set(re.findall(r'"(\w+)"\s*:', literal))

	def test_no_writer_names_a_field_the_row_does_not_have(self):
		for module, named in self.rows_written():
			with self.subTest(module, named=named):
				self.assertEqual(set(), named - ROW_FIELDS)

	def test_every_writer_says_which_server_the_row_is_for(self):
		for module, named in self.rows_written():
			with self.subTest(module, named=named):
				self.assertIn("server", named)
				self.assertIn("server_type", named)


if __name__ == "__main__":
	unittest.main()
