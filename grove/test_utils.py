# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Shared helpers. Pure — no site needed."""

import unittest

from grove.utils import slugify


class TestSlugify(unittest.TestCase):
	def test_slugs(self):
		cases = {
			"Qwen3.5 Coder": "qwen3.5-coder",  # dots kept — version digits matter
			"gpt_oss_120b": "gpt-oss-120b",
			"Qwen3.5  Coder_Next": "qwen3.5-coder-next",  # runs collapse to one dash
			"already-slugged": "already-slugged",
			"  Padded Name  ": "padded-name",
			"a - b": "a-b",  # spaced dash doesn't become '---'
			"-Leading-": "leading",
			"": "",  # caller throws on empty
		}
		for display_name, want in cases.items():
			self.assertEqual(slugify(display_name), want, display_name)


if __name__ == "__main__":
	unittest.main()
