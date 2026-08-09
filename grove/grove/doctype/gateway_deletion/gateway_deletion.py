# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GatewayDeletion(Document):
	"""One Redis record a proxy still holds and should not. Every other push is an UPSERT, so a
	revoked key or a deleted user would otherwise keep working on every box forever: a revoked key
	drops out of the projection instead of being restated, and a deleted row cannot be dirtied at
	all.

	Written on revoke or delete, pushed by the next sync_dirty, and dropped only once every Active
	proxy has acknowledged it. A proxy that was down when the key was revoked gets it on the tick
	after it comes back."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		record_id: DF.Data
		record_type: DF.Literal["Key", "User"]
	# end: auto-generated types


def record_deletion(record_type, record_id):
	"""Queue a Redis record for removal. Silent on a blank id: a key that never got a hash was
	never pushed, so there is nothing on any box to remove."""
	if not record_id:
		return
	frappe.get_doc(
		doctype="Gateway Deletion", record_type=record_type, record_id=record_id
	).insert(ignore_permissions=True)
