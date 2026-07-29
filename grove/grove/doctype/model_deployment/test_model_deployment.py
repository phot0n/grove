# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Engine env assembly. Pure — the deployment is passed in, so no site needed."""

import unittest
from types import SimpleNamespace

from grove.grove.doctype.model_deployment.model_deployment import _engine_env


def deployment(env=None, attention_backend="auto", allow_long_max_model_len=0):
	return SimpleNamespace(
		attention_backend=attention_backend,
		allow_long_max_model_len=allow_long_max_model_len,
		env=[SimpleNamespace(key=key, value=value) for key, value in (env or {}).items()],
	)


class TestEngineEnv(unittest.TestCase):
	def test_nothing_set_is_no_env(self):
		self.assertEqual(_engine_env(deployment(), ""), {})

	def test_derived_from_the_deployments_own_fields(self):
		md = deployment(attention_backend="FLASHINFER", allow_long_max_model_len=1)
		self.assertEqual(
			_engine_env(md, "hf_xxx"),
			{
				"HF_TOKEN": "hf_xxx",
				"VLLM_ATTENTION_BACKEND": "FLASHINFER",
				"VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1",
			},
		)

	def test_auto_attention_backend_is_left_to_vllm(self):
		self.assertNotIn("VLLM_ATTENTION_BACKEND", _engine_env(deployment(), ""))

	def test_operator_rows_win_over_the_derived_value(self):
		md = deployment({"VLLM_ATTENTION_BACKEND": "XFORMERS"}, attention_backend="FLASHINFER")
		self.assertEqual(_engine_env(md, "")["VLLM_ATTENTION_BACKEND"], "XFORMERS")

	def test_operator_rows_are_carried_through(self):
		env = _engine_env(deployment({"AWS_REGION": "us-east-1", "BLANK": None}), "")
		self.assertEqual(env["AWS_REGION"], "us-east-1")
		self.assertEqual(env["BLANK"], "")  # set-but-empty, not dropped


if __name__ == "__main__":
	unittest.main()
