from decimal import Decimal

import frappe
from frappe.model.document import Document

from grove.pricing import nano


class GatewaySpend(Document):
	"""What one Redis last told us about one user's spend: the audit input the push folds into that
	store's ceiling. Never billed from."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		balance_reported: DF.Currency
		drained_at: DF.Datetime | None
		gateway: DF.Link | None
		grove_user: DF.Link
		redis: DF.Data
		spent_known: DF.Currency
	# end: auto-generated types

	pass


def upsert(grove_user, redis, **values):
	"""One row per (user, Redis), written per pull."""
	name = frappe.db.exists("Gateway Spend", {"grove_user": grove_user, "redis": redis})
	values = {k: float(v) if isinstance(v, Decimal) else v for k, v in values.items()}
	if name:
		frappe.db.set_value("Gateway Spend", name, values)
		return name
	return frappe.get_doc(
		{"doctype": "Gateway Spend", "grove_user": grove_user, "redis": redis, **values}
	).insert(ignore_permissions=True).name


def spent_known(redis):
	"""{user: nano-USD} — the highest lifetime counter each user has reported from `redis`."""
	rows = frappe.get_all("Gateway Spend", filters={"redis": redis}, fields=["grove_user", "spent_known"])
	return {row.grove_user: nano(row.spent_known or 0) for row in rows}
