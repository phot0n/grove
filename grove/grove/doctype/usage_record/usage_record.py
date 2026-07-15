# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from datetime import datetime, timezone

import frappe
from frappe.model.document import Document


class UsageRecord(Document):
	def on_update(self):
		# TODO: a lot of usage records will do this at probably the same time
		self._enforce_budget()

	def _enforce_budget(self):
		"""When this month's recorded BILLABLE usage reaches the key's monthly token
		budget (API Key.max_tokens), flag the key rate_limited (+ dirty) so the next
		key sync tells every gateway to reject it with 429. Set-only here — clearing is
		the daily grove.usage_pull.reactivate_rate_limited job (which also breaks the
		month-rollover deadlock, since a blocked key sees no new usage to re-fire this).
		Reactive: usage is pulled after the fact, so a small overage is expected.

		Billable = total_tokens - cached_tokens: prefix-cache hits skip prefill compute
		(near-zero marginal cost on our own GPUs), so they don't count against budget.
		Keep this expression identical to reactivate_rate_limited's."""
		if self.month != datetime.now(timezone.utc).strftime("%Y-%m"):
			return  # only the current month gates
		limit = frappe.db.get_value("API Key", self.api_key, "max_tokens") or 0
		billable = (self.total_tokens or 0) - (self.cached_tokens or 0)
		if not limit or billable < limit:
			return
		if not frappe.db.get_value("API Key", self.api_key, "rate_limited"):
			frappe.db.set_value(
				"API Key",
				self.api_key,
				{"rate_limited": 1, "dirty": 1},
			)
