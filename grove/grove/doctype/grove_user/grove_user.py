# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GroveUser(Document):
	"""Grove's per-user policy: their groups, which models they may call, and how many tokens
	they may spend a month. All of it belongs to the person, not to any one API Key — a user's
	keys are just credentials and share this budget. Created on demand; no doc means no group
	and no allow, so the user reaches no models at all.

	The gateway holds it the same way: one user:<name> record that every key of theirs points at,
	so an access or budget change is a single write however many keys they hold. No sync hook
	anywhere here: the next tick's snapshot hash moves on any edit or delete and pushes it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_group_row.grove_group_row import GroveGroupRow
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		allow: DF.Table[GroveModelRow]
		deny: DF.Table[GroveModelRow]
		max_tokens: DF.Int
		rate_limited: DF.Check
		user: DF.Link
		user_groups: DF.TableMultiSelect[GroveGroupRow]
	# end: auto-generated types

	def validate(self):
		# Deny wins anyway, so a model on both lists is a mistake worth surfacing rather
		# than silently resolving.
		both = {row.model for row in self.allow} & {row.model for row in self.deny}
		if both:
			frappe.throw(f"{', '.join(sorted(both))} is in both Allow and Deny")


GROVE_USER_ROLE = "Grove User"


def register_user(email, full_name=None):
	"""The Website User behind `email`, created if nobody holds it yet, carrying the Grove User
	role. A policy is provisioned for someone who may never have signed in, and frappe checks the
	Link before any hook on this doctype runs — so the login is registered a step ahead of it, not
	from before_insert. The role is an identity marker with no perms; it grants nothing in Grove."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": full_name or email.split("@")[0],
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": GROVE_USER_ROLE}],
			}
		).insert(ignore_permissions=True)
	return email


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
	own cap. The next sync pushes one record, not one per key they hold.
	Returns True when something actually changed."""
	current = frappe.db.get_value("Grove User", grove_user, "rate_limited")
	if current is None or current == int(limited):
		return False
	# update_modified=False: a system flag flip is not a user edit and must not read as one.
	frappe.db.set_value("Grove User", grove_user, "rate_limited", int(limited), update_modified=False)
	return True
