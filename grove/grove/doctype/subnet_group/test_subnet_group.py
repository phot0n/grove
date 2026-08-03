# Copyright (c) 2026, Grove and contributors
# See license.txt
"""security_group_ids parsing. Pure — no site, box or AWS call."""

import unittest

from grove.grove.doctype.subnet_group.subnet_group import parse_security_group_ids


class TestParseSecurityGroupIds(unittest.TestCase):
	def test_splits_on_comma(self):
		self.assertEqual(parse_security_group_ids("sg-abc,sg-def"), ["sg-abc", "sg-def"])

	def test_strips_whitespace(self):
		self.assertEqual(parse_security_group_ids("sg-abc, sg-def , sg-ghi"),
						  ["sg-abc", "sg-def", "sg-ghi"])

	def test_blank_is_empty_list(self):
		self.assertEqual(parse_security_group_ids(""), [])
		self.assertEqual(parse_security_group_ids(None), [])

	def test_drops_empty_entries(self):
		self.assertEqual(parse_security_group_ids("sg-abc,,sg-def,"), ["sg-abc", "sg-def"])


if __name__ == "__main__":
	unittest.main()
