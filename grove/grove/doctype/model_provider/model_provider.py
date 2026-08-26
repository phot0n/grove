# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document

# The name prefixes every model id this provider serves — `frappe/qwen3.5-4b`, `anthropic/claude` —
# so it has to survive being typed into a JSON body by a customer.
PROVIDER_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class ModelProvider(Document):
	"""Who serves a model: `frappe` for our own engines, a vendor for a third-party API.

	The name is the whole record — it is the namespace every Model under it is named in, not a label.
	Renaming is off: the name is already inside every route key and every usage bucket a customer has
	been billed against.

	A Base URL is what makes a provider third-party: with one, a published Model under it routes
	straight to the vendor and no engine is ever started for it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password | None
		api_version: DF.Data | None
		base_url: DF.Data | None
	# end: auto-generated types

	def validate(self):
		if not PROVIDER_NAME.fullmatch(self.name or ""):
			frappe.throw(
				f"Provider name {self.name!r} must be lowercase letters, digits and single hyphens "
				"— it is the prefix of every model id this provider serves."
			)

		self.validate_endpoint()

	def validate_endpoint(self):
		"""A vendor is reachable only as a whole: an address, over TLS, with a credential."""
		if not self.base_url:
			return
		self.base_url = self.base_url.rstrip("/")
		if not self.base_url.startswith("https://"):
			# The key rides this hop. Plaintext would put it on the wire in the clear.
			frappe.throw(f"{self.name}'s Base URL must be https — it carries the API key.")
		if not self.get_password("api_key", raise_exception=False):
			frappe.throw(f"{self.name} has a Base URL but no API Key, so nothing could dial it.")

