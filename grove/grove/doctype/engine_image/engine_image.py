# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re

import requests

import frappe
from frappe.model.document import Document

# Docker Hub's registry API is not served from the hosts its refs name — `docker.io` is a
# website, and the CLI rewrites the host on its way out. Blank counts too: an image path with
# no host at all is Docker Hub's by convention. A one-segment repository there is shorthand for
# the library/ namespace, which the API does not accept.
DOCKER_HUB_REGISTRY = "registry-1.docker.io"
DOCKER_HUB_HOSTS = ("", "docker.io", "index.docker.io", "registry.hub.docker.com")
DOCKER_HUB_LIBRARY = "library"
MANIFEST_TYPES = (
	"application/vnd.oci.image.index.v1+json",
	"application/vnd.docker.distribution.manifest.list.v2+json",
	"application/vnd.oci.image.manifest.v1+json",
	"application/vnd.docker.distribution.manifest.v2+json",
)
# Every box Grove provisions is x86_64, so a multi-arch tag resolves to this one. Read the
# box's architecture instead if that stops being true.
IMAGE_ARCHITECTURE = "amd64"


class EngineImage(Document):
	"""A container image an engine (e.g. vLLM) is spawned from. The registry host and the
	pull credentials come from the linked Engine Image Provider, so they stay shared across
	every image in that registry."""

	def validate(self):
		self.full_image = self.get_full_image()

	def get_full_image(self):
		"""'<registry host>/<image path>' — the ref handed to the cloud provider. A path that
		already carries the host is left alone."""
		provider = frappe.get_cached_doc("Engine Image Provider", self.image_provider)
		host = (provider.registry_host or "").strip().rstrip("/")
		path = (self.image_path or "").strip().lstrip("/")
		if not host or path == host or path.startswith(f"{host}/"):
			return path
		return f"{host}/{path}"

	@property
	def registry_host(self):
		"""The registry to authenticate against, '' for Docker Hub."""
		return (frappe.get_cached_doc("Engine Image Provider", self.image_provider).registry_host or "").strip()

	@property
	def registry_credentials(self):
		"""(username, token) for the pull, or None when the registry is anonymous."""
		provider = frappe.get_cached_doc("Engine Image Provider", self.image_provider)
		token = provider.get_password("token", raise_exception=False)
		return (provider.username, token) if provider.username and token else None

	@property
	def is_docker_hub(self):
		"""Whether this image's registry is Docker Hub, under any of the hosts that name it."""
		return self.registry_host in DOCKER_HUB_HOSTS

	@property
	def api_registry_host(self):
		"""The host to make registry API calls against — the ref host for everyone except
		Docker Hub, whose API lives somewhere its refs never mention."""
		return DOCKER_HUB_REGISTRY if self.is_docker_hub else self.registry_host

	@frappe.whitelist()
	def fetch_size(self):
		"""Button: read what this image weighs off its own registry, so a box can be sized
		against a real number. This is the compressed download the manifest reports."""
		repository, reference = self.split_image_path()
		registry = self.api_registry_host
		manifest = self.get_manifest(registry, repository, reference)
		# A multi-arch tag points at one manifest per platform; the layers live one level down.
		if manifests := manifest.get("manifests"):
			digest = next(
				(
					entry["digest"]
					for entry in manifests
					if (entry.get("platform") or {}).get("architecture") == IMAGE_ARCHITECTURE
				),
				None,
			)
			if not digest:
				frappe.throw(f"{self.full_image} publishes no {IMAGE_ARCHITECTURE} image.")
			manifest = self.get_manifest(registry, repository, digest)

		layers = manifest.get("layers") or []
		if not layers:
			frappe.throw(f"{self.full_image}'s manifest lists no layers to measure.")
		size_gb = round(sum(layer.get("size") or 0 for layer in layers) / 1_000_000_000, 2)
		self.db_set("size_gb", size_gb)
		frappe.msgprint(f"{self.full_image}: {size_gb} GB over {len(layers)} layers.")
		return size_gb

	def split_image_path(self):
		"""'vllm/vllm-openai:v0.24.0' → ('vllm/vllm-openai', 'v0.24.0') — the two halves the
		registry API addresses a manifest by. The host is dropped when the path repeats it, a
		bare repository means :latest, and a one-segment name on Docker Hub is library/."""
		path = (self.image_path or "").strip().lstrip("/")
		if self.registry_host and path.startswith(f"{self.registry_host}/"):
			path = path[len(self.registry_host) + 1:]
		# A digest pins the manifest directly and carries its own colon, so it is split first.
		if "@" in path:
			repository, _, reference = path.partition("@")
		else:
			repository, _, reference = path.rpartition(":")
			# No colon at all leaves the repository empty; a colon belonging to a host:port
			# leaves a slash in what would be the tag. Neither is a reference.
			if not repository or "/" in reference:
				repository, reference = path, "latest"
		if self.is_docker_hub and "/" not in repository:
			repository = f"{DOCKER_HUB_LIBRARY}/{repository}"
		return repository, reference

	def get_manifest(self, registry, repository, reference):
		"""One manifest off the registry, authenticated for a pull."""
		response = requests.get(
			f"https://{registry}/v2/{repository}/manifests/{reference}",
			headers={
				"Accept": ", ".join(MANIFEST_TYPES),
				**self.pull_auth_header(registry, repository),
			},
			timeout=30,
		)
		if not response.ok:
			frappe.throw(
				f"{registry} returned {response.status_code} for {repository}:{reference} — the "
				"image is missing, or the provider's credentials do not reach it."
			)
		return registry_json(response, f"{registry}'s manifest for {repository}")

	def pull_auth_header(self, registry, repository):
		"""Authorization for a pull, via the registry's own auth challenge — the same flow
		every OCI registry implements, so Docker Hub, GHCR and a private one need no branching.
		A registry that does not challenge (or does not use Bearer) gets no header."""
		challenge = requests.get(f"https://{registry}/v2/", timeout=30).headers.get(
			"Www-Authenticate", ""
		)
		if not challenge.startswith("Bearer "):
			return {}
		params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
		if not params.get("realm"):
			return {}
		response = requests.get(
			params["realm"],
			params={"service": params.get("service", ""), "scope": f"repository:{repository}:pull"},
			auth=self.registry_credentials,
			timeout=30,
		)
		if not response.ok:
			frappe.throw(f"{registry} refused a pull token for {repository} ({response.status_code}).")
		body = registry_json(response, f"{registry}'s token endpoint")
		token = body.get("token") or body.get("access_token")
		return {"Authorization": f"Bearer {token}"} if token else {}


def registry_json(response, what):
	"""A registry response's JSON body, or a readable failure. A host that is not a registry
	API answers 200 with an HTML page, and json() alone reports only that it choked on a '<'."""
	try:
		return response.json()
	except ValueError:
		frappe.throw(
			f"{what} did not return JSON — it answered "
			f"{response.headers.get('Content-Type') or 'with no content type'}. Check the Engine "
			"Image Provider's Registry Host: a registry's API is rarely the host its images are "
			"named after."
		)
