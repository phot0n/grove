# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Shared helpers. Pure — no site needed."""

import re
import unittest

from grove.utils import (
	is_dns_name,
	is_env_key,
	is_env_value,
	is_id_safe,
	is_label_under,
	slugify,
	validate_id_safe_name,
)


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
		for typed, want in cases.items():
			self.assertEqual(slugify(typed), want, typed)


class TestIsIdSafe(unittest.TestCase):
	"""Gateway Server and Inference Server names are typed by an operator and end up inside a
	request id, which the gateway builds by rewriting '-' to '_'. The rule exists so that
	rewrite can be undone."""

	def test_letters_digits_and_dashes_are_fine(self):
		for name in ("inf-blackwell", "proxy-sg", "INF1", "a"):
			self.assertTrue(is_id_safe(name), name)

	def test_an_underscore_is_refused(self):
		# The one that matters: 'inf_a' and 'inf-a' both reach an id as 'inf_a', so the id no
		# longer names one box.
		for name in ("inf_a", "_leading", "trailing_"):
			self.assertFalse(is_id_safe(name), name)

	def test_anything_cleanidpart_would_drop_is_refused(self):
		# Same bug, quieter: these lose characters rather than colliding.
		for name in ("inf.a", "inf a", "inf/a", "inf:a", "inf+a"):
			self.assertFalse(is_id_safe(name), name)

	def test_an_empty_name_is_refused(self):
		for name in ("", None):
			self.assertFalse(is_id_safe(name), repr(name))

	def test_a_safe_name_is_recoverable_from_the_id_it_produces(self):
		# Mirror of CleanIDPart (pathway, internal/domain/requestid.go): keep [A-Za-z0-9_],
		# '-' → '_', drop
		# the rest. For an id-safe name that is the ONLY transformation, so swapping back
		# recovers the doc name — which is the whole point of the rule.
		def clean_id_part(value):
			return re.sub(r"[^A-Za-z0-9_-]", "", value).replace("-", "_") or "x"

		for name in ("inf-blackwell", "proxy-sg-1", "plain"):
			self.assertEqual(clean_id_part(name).replace("_", "-"), name)
		# And the collision the rule exists to prevent: these are one id part, not two.
		self.assertEqual(clean_id_part("inf_a"), clean_id_part("inf-a"))


class TestValidateIdSafeName(unittest.TestCase):
	"""The gate itself, as before_insert and before_rename call it. frappe.throw needs a bound
	site, so what is asserted is that the call does not return."""

	def test_a_bad_name_does_not_get_through(self):
		with self.assertRaises(Exception):
			validate_id_safe_name("Inference Server", "inf_a")

	def test_a_good_name_passes(self):
		validate_id_safe_name("Inference Server", "inf-a")

	def test_a_blank_name_is_left_to_frappe(self):
		# Frappe raises "Name is required" straight after, and says it better than this would.
		for blank in ("", None):
			validate_id_safe_name("Inference Server", blank)


class TestIsDnsName(unittest.TestCase):
	"""What can go in an nginx server_name and a certificate subject. Everything rejected here
	would otherwise be caught by `openresty -t` on a box that is already serving traffic."""

	def test_accepts_a_bare_name(self):
		for name in ("grove.example.com", "api.grove.example.com", "use1-p1.grove.example.com", "localhost"):
			self.assertTrue(is_dns_name(name), name)

	def test_rejects_anything_that_is_not_one(self):
		for name in ("", None, "https://api.grove.example.com", "api.grove.example.com/",
			"api.grove.example.com:443", "grove.example.com.", "api..grove.example.com",
			"-api.grove.example.com", "api_1.grove.example.com"):
			self.assertFalse(is_dns_name(name), name)


class TestIsLabelUnder(unittest.TestCase):
	"""A wildcard covers one label and no more, which is the whole constraint on Gateway Host:
	*.grove.example.com is presented by every proxy, and a name it does not match reaches a
	customer's SDK as a certificate error days after someone typed it."""

	def test_one_label_below_the_zone(self):
		self.assertTrue(is_label_under("api.grove.example.com", "grove.example.com"))

	def test_the_zone_itself_is_not_covered(self):
		self.assertFalse(is_label_under("grove.example.com", "grove.example.com"))

	def test_two_labels_below_are_not_covered(self):
		self.assertFalse(is_label_under("api.eu.grove.example.com", "grove.example.com"))

	def test_a_name_in_another_zone_is_not_covered(self):
		# ...including one that merely ends the same way.
		self.assertFalse(is_label_under("api.notgrove.example.com", "grove.example.com"))


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
