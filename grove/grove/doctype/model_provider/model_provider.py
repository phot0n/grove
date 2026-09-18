# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document

# Prefixes every model id this provider serves, so it has to survive being typed into a JSON body
# by a customer.
PROVIDER_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class ModelProvider(Document):
	"""Who serves a model: the one flagged Self Hosted for our own engines, a vendor for a
	third-party API.

	The name IS the record — the namespace every Model under it is named in, not a label. Renaming
	is off: it is already inside every route key and every usage bucket a customer was billed
	against.

	Self Hosted names ours: a Model with no provider is named under it, and only its models can be
	deployed. Every other provider is a vendor: a published Model there routes straight to it and no
	engine is ever started. The URL fields are the dialect declaration — one per front the vendor
	runs (OpenAI-compatible, Anthropic-compatible), either or both."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		anthropic_base_url: DF.Data | None
		api_key: DF.Password | None
		api_version: DF.Data | None
		base_url: DF.Data | None
		geography: DF.Link | None
		is_self_hosted: DF.Check
	# end: auto-generated types

	def validate(self):
		if not PROVIDER_NAME.fullmatch(self.name or ""):
			frappe.throw(
				f"Provider name {self.name!r} must be lowercase letters, digits and single hyphens "
				"— it is the prefix of every model id this provider serves."
			)

		# mandatory_depends_on is client-side only; this is the gate an API insert hits.
		if (self.base_url or self.anthropic_base_url) and not self.geography:
			frappe.throw(f"{self.name} needs a Geography — only gateways in it may route to this vendor.")

		if self.is_self_hosted:
			self.validate_self_hosted()
		# self.validate_endpoint()

	def validate_self_hosted(self):
		"""One provider is ours, and it dials nothing — a URL is what makes a vendor."""
		if self.base_url or self.anthropic_base_url:
			frappe.throw(f"{self.name} is Self Hosted, so there is no vendor URL to dial.")
		other = frappe.db.get_value(
			"Model Provider", {"is_self_hosted": 1, "name": ("!=", self.name)}, "name"
		)
		if other:
			frappe.throw(f"{other} is already the Self Hosted provider — there is one.")

	def on_update(self):
		# The mirror on Model is what its form and the Model link filters read.
		if self.has_value_changed("is_self_hosted"):
			frappe.db.set_value(
				"Model", {"provider": self.name}, "provider_is_self_hosted", self.is_self_hosted
			)

	def validate_endpoint(self):
		"""A vendor is reachable only as a whole: an address, over TLS, with a credential.

		ponytail: parked — the call in validate() is commented out."""
		if not self.base_url:
			return
		self.base_url = self.base_url.rstrip("/")
		if not self.base_url.startswith("https://"):
			# The key rides this hop; plaintext would put it on the wire in the clear.
			frappe.throw(f"{self.name}'s Base URL must be https — it carries the API key.")
		if not self.get_password("api_key", raise_exception=False):
			frappe.throw(f"{self.name} has a Base URL but no API Key, so nothing could dial it.")


def self_hosted_provider():
	"""The one provider our own engines serve under, None until one is flagged."""
	return frappe.db.get_value("Model Provider", {"is_self_hosted": 1}, "name")
