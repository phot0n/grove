# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import hashlib
import secrets

import frappe
from frappe.model.document import Document

KEY_PREFIX = "gr_sk_"


def hash_secret(secret: str) -> str:
	"""The Redis key id the gateway uses = sha256 of the full presented key."""
	return hashlib.sha256(secret.encode()).hexdigest()


class GroveAPIKey(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_secret: DF.Password | None
		dirty: DF.Check
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

	def on_update(self):
		# Covers revocation (status flip) and any other key edit.
		if not self.dirty:
			self._mark_dirty()

	def revoke(self):
		# this guards against weird race condiions where the key is created and revoked at the same time.
		if frappe.utils.time_diff_in_hours(frappe.utils.now_datetime(), self.creation) < 6:
			frappe.throw("API key cannot be revoked less than 6 hours of it's creation")

		self.status = "revoked"
		self.dirty = 1
		self.save(ignore_permissions=True)

	def _mark_dirty(self):
		frappe.db.set_value(self.doctype, self.name, "dirty", 1, update_modified=False)
		self.dirty = 1


def mark_keys_dirty(grove_users):
	"""Queue keys for the next sync_dirty push. A key carries its user's flattened model
	set, so anything that changes access has to re-push the affected keys. Takes Grove User
	doc names (what `user` links to). `grove_users=None` means every key — what a Model
	release does, since it moves for everyone."""
	filters = {"dirty": 0}
	if grove_users is not None:
		grove_users = list(grove_users)
		if not grove_users:
			return
		filters["user"] = ("in", grove_users)
	frappe.db.set_value("Grove API Key", filters, "dirty", 1, update_modified=False)
