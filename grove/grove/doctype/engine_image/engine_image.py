# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


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
	def registry_credentials(self):
		"""(username, token) for the pull, or None when the registry is anonymous."""
		provider = frappe.get_cached_doc("Engine Image Provider", self.image_provider)
		token = provider.get_password("token", raise_exception=False)
		return (provider.username, token) if provider.username and token else None
