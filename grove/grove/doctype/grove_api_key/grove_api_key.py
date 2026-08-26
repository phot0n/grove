# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import hashlib
import secrets

import frappe
from frappe.model.document import Document

KEY_PREFIX = "gr_"


def hash_secret(secret: str) -> str:
	"""The Redis key id the gateway uses = sha256 of the full presented key."""
	return hashlib.sha256(secret.encode()).hexdigest()


class GroveAPIKey(Document):
	"""A credential and nothing more — access and budget live on the Grove User it points at.

	No sync hook: only active keys are projected, so revoking or deleting one moves its bucket's
	snapshot hash and the next tick prunes it off every box."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_secret: DF.Password | None
		key_hash: DF.Data | None
		status: DF.Literal["active", "revoked"]
		user: DF.Link
	# end: auto-generated types

	def before_insert(self):
		# Mint the secret once. key_hash (sha256) is what the gateway keys on and
		# what revoke looks up; api_secret holds the full key in a Password field
		# (encrypted at rest) for reveal-later. key_prefix is display-only.
		full_key = KEY_PREFIX + secrets.token_hex(24)
		self.key_hash = hash_secret(full_key)
		self.api_secret = full_key

	@frappe.whitelist()
	def revoke(self):
		"""Retire the credential. The row stays as the record that it existed and when it
		stopped; the gateways' copy goes when the next sync prunes the unprojected key."""
		# this guards against weird race condiions where the key is created and revoked at the same time.
		if frappe.utils.time_diff_in_hours(frappe.utils.now_datetime(), self.creation) < 6:
			frappe.throw("API key cannot be revoked less than 6 hours of it's creation")

		self.status = "revoked"
		self.save(ignore_permissions=True)
