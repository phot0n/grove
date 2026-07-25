# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""ServeCommand argument assembly. Pure — the Model row is passed in, so no site needed."""

import unittest

from grove.serve_command import ServeCommand

CHAT_MODEL = {
	"hf_repo": "Qwen/Qwen3-35B",
	"quantization": "fp8",
	"modality": "text",
	"enable_prefix_caching": True,
	"enable_auto_tool_choice": True,
	"tool_call_parser": "hermes",
	"thinking": True,
	"reasoning_parser": "qwen3",
}


def serve(model=None, **tuning):
	tuning.setdefault("port", 8080)
	return ServeCommand("qwen3-35b", model if model is not None else dict(CHAT_MODEL), **tuning)


class TestServeCommand(unittest.TestCase):
	def test_chat_model_flags(self):
		args = serve(tensor_parallel_size=2, max_model_len=32768).args
		self.assertEqual(args[:3], ["--served-model-name", "qwen3-35b", "--host"])
		for flag, value in (
			("--port", "8080"),
			("--tensor-parallel-size", "2"),
			("--max-model-len", "32768"),
			("--quantization", "fp8"),
			("--tool-call-parser", "hermes"),
			("--reasoning-parser", "qwen3"),
		):
			self.assertEqual(args[args.index(flag) + 1], value, flag)
		for flag in ("--language-model-only", "--enable-prefix-caching", "--enable-auto-tool-choice"):
			self.assertIn(flag, args)

	def test_defaults_when_tuning_blank(self):
		args = serve().args
		self.assertEqual(args[args.index("--max-model-len") + 1], "8192")
		self.assertEqual(args[args.index("--gpu-memory-utilization") + 1], "0.9")
		self.assertEqual(args[args.index("--tensor-parallel-size") + 1], "1")
		self.assertNotIn("--dtype", args)  # dtype auto → vLLM decides

	def test_embedding_model_drops_chat_flags(self):
		model = dict(CHAT_MODEL, is_embedding=True)
		args = serve(model).args
		for flag in ("--enable-auto-tool-choice", "--tool-call-parser", "--reasoning-parser"):
			self.assertNotIn(flag, args)
		self.assertIn("--enable-prefix-caching", args)  # not chat-only

	def test_thinking_off_drops_reasoning_parser(self):
		args = serve(dict(CHAT_MODEL, thinking=False)).args
		self.assertNotIn("--reasoning-parser", args)

	def test_aliases_and_extra_args(self):
		args = serve(aliases="old-name, older-name", extra_serve_args="--kv-cache-dtype fp8").args
		self.assertEqual(args[:4], ["--served-model-name", "qwen3-35b", "old-name", "older-name"])
		self.assertEqual(args[-2:], ["--kv-cache-dtype", "fp8"])  # appended verbatim, last

	def test_command_is_repo_then_args(self):
		command = serve().command
		self.assertTrue(command.startswith("Qwen/Qwen3-35B --served-model-name qwen3-35b "))

	def test_command_empty_without_repo(self):
		self.assertEqual(serve(dict(CHAT_MODEL, hf_repo=None)).command, "")


if __name__ == "__main__":
	unittest.main()
