from decimal import Decimal

import frappe
from frappe.model.document import Document


class CreditDiscrepancy(Document):
	"""Where the gateway's copy of the money and the control plane disagreed. Read to find out why;
	never read to decide."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link | None
		counter: DF.Literal['', 'input_tokens', 'cached_tokens', 'cache_write_tokens', 'cache_write_1h_tokens', 'completion_tokens', 'audio_seconds', 'request_count']
		delta: DF.Currency
		expected_value: DF.Currency
		gateway: DF.Link | None
		gateway_value: DF.Currency
		grove_user: DF.Link | None
		kind: DF.Literal['Price Drift', 'Balance Mismatch', 'Counter Reset', 'Overspend', 'Ledger Drift']
		model: DF.Link | None
		note: DF.SmallText | None
		redis: DF.Data | None
		resolved: DF.Check
	# end: auto-generated types

	pass


# What makes a row of each kind the same row: drift is a property of the rate, a balance of the
# (user, Redis), an overspend of the user.
OPEN_KEY = {
	"Price Drift": ("model", "counter", "redis"),
	"Balance Mismatch": ("grove_user", "redis"),
	"Counter Reset": ("grove_user", "redis"),
	"Overspend": ("grove_user",),
	"Ledger Drift": ("grove_user",),
}


def open_row(kind, facts):
	filters = {"kind": kind, "resolved": 0}
	for field in OPEN_KEY[kind]:
		filters[field] = facts.get(field) or ("is", "not set")
	return frappe.db.exists("Credit Discrepancy", filters)


def record(kind, **facts):
	"""The one writer. One open row per kind and key, updated rather than piled. → its name."""
	facts = {k: float(v) if isinstance(v, Decimal) else v for k, v in facts.items()}
	name = open_row(kind, facts)
	if name:
		frappe.db.set_value("Credit Discrepancy", name, facts, update_modified=True)
		return name
	return frappe.get_doc({"doctype": "Credit Discrepancy", "kind": kind, **facts}).insert(ignore_permissions=True).name


def resolve(kind, **key):
	"""Close the open row for this key, if there is one."""
	name = open_row(kind, key)
	if name:
		frappe.db.set_value("Credit Discrepancy", name, "resolved", 1)
	return name
