# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from datetime import datetime, timezone

import frappe
from frappe.model.document import Document

from grove.grove.doctype.grove_user.grove_user import monthly_budget, set_rate_limited


class UsageRecord(Document):
	def on_update(self):
		# TODO: a lot of usage records will do this at probably the same time
		self._enforce_budget()

	def _enforce_budget(self):
		"""When the USER's recorded billable usage this month reaches their budget
		(Grove User.max_tokens), flag every key they hold rate_limited (+ dirty) so the
		next key sync tells the gateways to reject them with 429. The budget belongs to
		the person and is shared across their keys, so one key exhausting it stops the
		lot. Set-only here — clearing is the daily grove.usage_pull.reactivate_rate_limited
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


def billable_tokens(grove_user, month):
	"""What `grove_user` spent in `month`, summed across every key they hold.

	Billable = total_tokens - cached_tokens: prefix-cache hits skip prefill compute
	(near-zero marginal cost on our own GPUs), so they don't count against budget. One
	definition, shared by the set and the clear side of the budget gate."""
	rows = frappe.get_all(
		"Usage Record",
		filters={"user": grove_user, "month": month},
		fields=["total_tokens", "cached_tokens"],
	)
	return sum((r.total_tokens or 0) - (r.cached_tokens or 0) for r in rows)
