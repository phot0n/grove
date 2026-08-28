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
		total_tokens: DF.Int
		user: DF.Link | None
	# end: auto-generated types

	def on_update(self):
		# TODO: a lot of usage records will do this at probably the same time
		self._enforce_budget()

	def _enforce_budget(self):
		"""When the USER's recorded billable usage this month reaches their budget
		(Grove User.max_tokens), flag the USER rate_limited so the next sync tells
		the gateways to reject them with 429. The budget belongs to the person and is shared
		across their keys, so one key exhausting it stops the lot — and one record does it,
		however many they hold. Set-only here — clearing is the daily grove.usage_pull.reactivate_rate_limited
		job (which also breaks the month-rollover deadlock, since a blocked user sees no
		new usage to re-fire this). Reactive: usage is pulled after the fact, so a small
		overage is expected."""
		if self.month != current_month():
			return  # only the current month gates
		limit = monthly_budget(self.user)
		if not limit or billable_tokens(self.user, self.month) < limit:
			return
		set_rate_limited(self.user, 1)


def current_month():
	"""The billing month off OUR clock, UTC — matches grove.api.usage."""
	return datetime.now(timezone.utc).strftime("%Y-%m")


def billable(total_tokens, cached_tokens):
	"""One record's billable tokens: total minus the prefix-cache hits, never below zero.

	Cache hits skip prefill compute — near-zero marginal cost on our own GPUs — so they do not
	count against a budget. Cached is a SUBSET of total, so the difference should never be
	negative; the floor is there for when it is anyway. A response can report cached tokens with
	total_tokens: 0, and the gateway's /meter skips a zero rather than writing it, leaving a
	record whose cached count exceeds its total. Summed unclamped, that row would cancel real
	usage on the user's OTHER keys and quietly credit them back under their budget.

	The one definition of billable: the budget gate and the usage report both come here, so they
	cannot drift into disagreeing about what a customer owes."""
	return max((total_tokens or 0) - (cached_tokens or 0), 0)


def billable_tokens(grove_user, month):
	"""What `grove_user` spent in `month`, summed across every key they hold. One definition,
	shared by the set and the clear side of the budget gate."""
	rows = frappe.get_all(
		"Usage Record",
		filters={"user": grove_user, "month": month},
		fields=["total_tokens", "cached_tokens"],
	)
	return sum(billable(r.total_tokens, r.cached_tokens) for r in rows)
