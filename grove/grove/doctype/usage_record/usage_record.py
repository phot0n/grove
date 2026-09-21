# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from datetime import datetime, timezone

import frappe
from frappe.model.document import Document

from grove.grove.doctype.grove_user.grove_user import monthly_budget, set_rate_limited


class UsageRecord(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.usage_gateway_row.usage_gateway_row import UsageGatewayRow
		from grove.grove.doctype.usage_model_row.usage_model_row import UsageModelRow

		api_key: DF.Link
		cached_tokens: DF.Int
		completion_tokens: DF.Int
		gateway_usage: DF.Table[UsageGatewayRow]
		model_usage: DF.Table[UsageModelRow]
		month: DF.Data
		prompt_tokens: DF.Int
		request_count: DF.Int
		user: DF.Link | None
	# end: auto-generated types

def enforce_budget(grove_user, month):
	"""Flag the USER rate_limited once their billable usage this month reaches max_tokens, so the
	next sync has the gateways 429 them. The budget is the person's and shared across their keys,
	so one key exhausting it stops the lot. Called by the usage pull once per user it touched —
	not per record, which is what an on_update hook cost.

	Set-only: clearing is the daily reactivate_rate_limited job, which is also what breaks the
	month-rollover deadlock — a blocked user sees no new usage to re-fire this. Reactive, so a
	small overage is expected."""
	if month != current_month():
		return False  # only the current month gates
	limit = monthly_budget(grove_user)
	if not limit or billable_tokens(grove_user, month) < limit:
		return False
	return set_rate_limited(grove_user, 1)


def current_month():
	"""The billing month off OUR clock, UTC — matches grove.api.usage."""
	return datetime.now(timezone.utc).strftime("%Y-%m")


def billable(prompt_tokens, completion_tokens, cached_tokens):
	"""Uncached prompt plus completion. Cached ⊆ prompt, but floored so a record whose cached
	count exceeds its prompt (the gateway skips zeros) cannot credit a user's other keys."""
	return max((prompt_tokens or 0) - (cached_tokens or 0), 0) + (completion_tokens or 0)


def billable_tokens(grove_user, month):
	"""What `grove_user` spent in `month`, summed across every key they hold. One definition,
	shared by the set and the clear side of the budget gate."""
	rows = frappe.get_all(
		"Usage Record",
		filters={"user": grove_user, "month": month},
		fields=["prompt_tokens", "completion_tokens", "cached_tokens"],
	)
	return sum(billable(r.prompt_tokens, r.completion_tokens, r.cached_tokens) for r in rows)
