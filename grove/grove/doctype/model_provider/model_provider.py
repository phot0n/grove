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
	been billed against. Endpoint and credential fields arrive with the first third-party route, since
	nothing reads them until something dials out."""

	def validate(self):
		if not PROVIDER_NAME.fullmatch(self.name or ""):
			frappe.throw(
				f"Provider name {self.name!r} must be lowercase letters, digits and single hyphens "
				"— it is the prefix of every model id this provider serves."
			)
