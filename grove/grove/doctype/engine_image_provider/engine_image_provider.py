# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class EngineImageProvider(Document):
	"""A container registry (ghcr.io, docker.io, a private one) and the credentials to pull
	from it. Registry-agnostic: one record per registry, shared by its Engine Images."""

	pass
