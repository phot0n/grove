# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.grove.doctype.grove_api_key.grove_api_key import mark_keys_dirty


class GroveUser(Document):
	"""Grove's per-user policy: their group, which models they may call, and how many tokens
	they may spend a month. All of it belongs to the person, not to any one API Key — a user's
	keys are just credentials and share this budget. Created on demand; no doc means no group
	and no allow, so the user reaches no models at all."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		allow: DF.Table[GroveModelRow]
		deny: DF.Table[GroveModelRow]
		max_tokens: DF.Int
		rate_limited: DF.Check
		user: DF.Link
		user_group: DF.Link | None
	# end: auto-generated types

	def validate(self):
		# Deny wins anyway, so a model on both lists is a mistake worth surfacing rather
		# than silently resolving.
		both = {row.model for row in self.allow} & {row.model for row in self.deny}
		if both:
			frappe.throw(f"{', '.join(sorted(both))} is in both Allow and Deny")

	def on_update(self):
		# Access and budget both ride on the user's keys, so re-push all of them.
		mark_keys_dirty([self.name])

	def on_trash(self):
		mark_keys_dirty([self.name])


def for_email(email):
	"""The Grove User name behind a login email, or None. Only the outward-facing edges
	speak email — everything downstream of a key carries this name instead."""
	return frappe.db.get_value("Grove User", {"user": email}) if email else None


def monthly_budget(grove_user):
	"""total_tokens this user may spend per calendar month, 0 = unlimited."""
	return frappe.db.get_value("Grove User", grove_user, "max_tokens") or 0


def set_rate_limited(grove_user, limited):
	"""Flip the 429 gate for `grove_user`. Held here, not on the keys, because the budget is
	the person's — storing it per key let a blocked user mint a fresh one and walk past their
	own cap. Their keys project it (gateway_sync) and are dirtied so the next sync pushes
	status. Returns True when something actually changed."""
	current = frappe.db.get_value("Grove User", grove_user, "rate_limited")
	if current is None or current == int(limited):
		return False
	frappe.db.set_value("Grove User", grove_user, "rate_limited", int(limited), update_modified=False)
	mark_keys_dirty([grove_user])
	return True


def is_rate_limited(grove_user):
	"""Whether `grove_user` is over budget — projected onto each of their keys as a status."""
	return bool(frappe.db.get_value("Grove User", grove_user, "rate_limited"))
