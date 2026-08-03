# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Shared helpers. Pure — no site needed."""

import unittest

from grove.utils import is_env_key, is_env_value, slugify


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


class TestIsEnvKey(unittest.TestCase):
	def test_accepts_posix_names(self):
		for name in ("HF_TOKEN", "_private", "VLLM_ATTENTION_BACKEND", "a", "X2"):
			self.assertTrue(is_env_key(name), name)

	def test_rejects_anything_a_shell_or_unit_file_would_reparse(self):
		# These end up in a systemd unit and a `docker run` argv.
		for name in ("", None, "2FOO", "FOO-BAR", "FOO BAR", "FOO=BAR", "FOO;rm -rf /", "FOO\nBAR"):
			self.assertFalse(is_env_key(name), name)


class TestIsEnvValue(unittest.TestCase):
	def test_accepts_ordinary_values(self):
		for value in ("", None, "hf_xxx", "a b;c", "s3://bucket/key?x=1", "'quoted'", "a\tb"):
			self.assertTrue(is_env_value(value), value)

	def test_rejects_a_value_that_could_append_a_unit_directive(self):
		# Rendered as Environment="KEY=<value>" — a newline starts a new directive.
		self.assertFalse(is_env_value("a\nExecStart=/bin/evil"))
		self.assertFalse(is_env_value("a\rExecStart=/bin/evil"))
		self.assertFalse(is_env_value('ends" ExecStart=/bin/evil'))


if __name__ == "__main__":
	unittest.main()
