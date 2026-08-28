# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Reading a repo's config.json for what the weights will be served in.

Pure — the config is passed in, so no repo and no site. Worth pinning on its own because the answer
feeds a HARD placement filter, and getting it wrong is silent in the worst direction: a blank dtype
reads as "unknown", the capability check skips, and a T4 is offered a model it cannot run."""

import unittest

from grove.grove.doctype.model.model import config_dtype

# Qwen3.5-4B, trimmed. Multimodal, transformers 4.57 — the dtype is on the LANGUAGE model and
# spelled `dtype`, and there is nothing at the top level. This exact shape returned "" from a
# top-level-only read, which is how the check shipped inert the first time.
QWEN35_4B = {
	"architectures": ["Qwen3_5ForConditionalGeneration"],
	"model_type": "qwen3_5",
	"text_config": {"dtype": "bfloat16", "num_attention_heads": 16, "num_hidden_layers": 32},
	"vision_config": {"hidden_size": 1024},
}


class TestConfigDtype(unittest.TestCase):
	def test_a_multimodal_repo_states_it_on_the_language_model(self):
		shape = QWEN35_4B["text_config"]
		self.assertEqual(config_dtype(QWEN35_4B, shape), "bfloat16")

	def test_the_top_level_alone_finds_nothing_here(self):
		# The bug, kept as a test: the nesting is not optional to handle.
		self.assertEqual(config_dtype(QWEN35_4B), "")

	def test_both_spellings_are_read(self):
		# transformers renamed torch_dtype to dtype in 4.57; repos on either side of that are
		# both in the fleet.
		self.assertEqual(config_dtype({"torch_dtype": "float16"}), "float16")
		self.assertEqual(config_dtype({"dtype": "float16"}), "float16")

	def test_the_language_model_wins_over_the_top_level(self):
		# The top level describes the whole thing, vision tower included; what vLLM shards for
		# --language-model-only is the text config, so that is the authority.
		config = {"torch_dtype": "float32", "text_config": {"dtype": "bfloat16"}}
		self.assertEqual(config_dtype(config, config["text_config"]), "bfloat16")

	def test_a_repo_that_says_nothing_says_nothing(self):
		# Blank is "unknown", which skips the check rather than failing it — a repo with no dtype
		# must not become unplaceable.
		self.assertEqual(config_dtype({"model_type": "llama"}), "")
		self.assertEqual(config_dtype({}, {}), "")


if __name__ == "__main__":
	unittest.main()
