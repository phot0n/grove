# Copyright (c) 2026, Grove and contributors
# See license.txt
"""Splitting an image ref into what the registry API addresses a manifest by. Pure — the ref
is passed in, so no site and no registry call."""

import unittest
from types import SimpleNamespace

from grove.grove.doctype.engine_image.engine_image import DOCKER_HUB_HOSTS, EngineImage


def image(image_path, registry_host=""):
	"""A stand-in Engine Image carrying just what the split reads — the two properties are
	shadowed here, since the real ones reach for the Engine Image Provider."""
	return SimpleNamespace(
		image_path=image_path,
		registry_host=registry_host,
		is_docker_hub=registry_host in DOCKER_HUB_HOSTS,
	)


def split(image_path, registry_host=""):
	return EngineImage.split_image_path(image(image_path, registry_host))


class TestSplitImagePath(unittest.TestCase):
	def test_a_tagged_docker_hub_image(self):
		self.assertEqual(split("vllm/vllm-openai:v0.24.0"), ("vllm/vllm-openai", "v0.24.0"))

	def test_an_untagged_image_is_latest(self):
		self.assertEqual(split("vllm/vllm-openai"), ("vllm/vllm-openai", "latest"))

	def test_a_one_segment_docker_hub_name_is_under_library(self):
		# The registry API has no shorthand — `ubuntu` is `library/ubuntu` on the wire.
		self.assertEqual(split("ubuntu:22.04"), ("library/ubuntu", "22.04"))
		self.assertEqual(split("ubuntu"), ("library/ubuntu", "latest"))

	def test_docker_hub_named_by_host_is_still_docker_hub(self):
		# The provider spells its Registry Host `docker.io`, which is a website, not the API —
		# and the library/ shorthand still has to be expanded for it.
		self.assertEqual(
			split("vllm/vllm-openai:v0.24.0", registry_host="docker.io"),
			("vllm/vllm-openai", "v0.24.0"),
		)
		self.assertEqual(split("ubuntu:22.04", registry_host="docker.io"), ("library/ubuntu", "22.04"))
		self.assertEqual(
			split("docker.io/vllm/vllm-openai:v1", registry_host="docker.io"),
			("vllm/vllm-openai", "v1"),
		)

	def test_a_private_registry_drops_the_host_it_already_carries(self):
		self.assertEqual(
			split("ghcr.io/acme/vllm:v1", registry_host="ghcr.io"), ("acme/vllm", "v1")
		)

	def test_a_private_registry_path_without_the_host(self):
		self.assertEqual(split("acme/vllm:v1", registry_host="ghcr.io"), ("acme/vllm", "v1"))

	def test_a_one_segment_name_is_left_alone_off_docker_hub(self):
		# library/ is Docker Hub's convention, not the protocol's.
		self.assertEqual(split("vllm:v1", registry_host="ghcr.io"), ("vllm", "v1"))

	def test_a_host_port_colon_is_not_mistaken_for_a_tag(self):
		self.assertEqual(
			split("registry.local:5000/acme/vllm", registry_host="registry.local:5000"),
			("acme/vllm", "latest"),
		)

	def test_a_digest_ref_keeps_its_whole_digest(self):
		digest = "sha256:f9de5cd9fa9071a2b1b0e2a0d7b39e1c" + "0" * 32
		self.assertEqual(
			split(f"vllm/vllm-openai@{digest}"), ("vllm/vllm-openai", digest)
		)


if __name__ == "__main__":
	unittest.main()
