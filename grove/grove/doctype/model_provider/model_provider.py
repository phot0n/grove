# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document

# Prefixes every model id this provider serves, so it has to survive being typed into a JSON body
# by a customer.
PROVIDER_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class ModelProvider(Document):
	"""Who serves a model: `frappe` for our own engines, a vendor for a third-party API.

	The name IS the record — the namespace every Model under it is named in, not a label. Renaming
	is off: it is already inside every route key and every usage bucket a customer was billed
	against.

	A base URL is what makes a provider third-party: with one, a published Model routes straight to
	the vendor and no engine is ever started. The URL fields are also the dialect declaration —
	one per front the vendor runs (OpenAI-compatible, Anthropic-compatible), either or both."""

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

		# self.validate_endpoint()

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
