"""What one drained Redis said about the money, checked against what Grove prices — every pull.

Grove never bills from a gateway figure: the deltas are priced here at today's tables and
`spent` moves by that. The box's own `cost`, `user_spent` and `user_balance` are audit inputs;
where they disagree with the control plane a Credit Discrepancy row says so."""

from decimal import Decimal

import frappe

from grove.grove.doctype.credit_discrepancy.credit_discrepancy import record
from grove.grove.doctype.gateway_spend.gateway_spend import spent_known, upsert
from grove.pricing import NANO, settle, tolerance


def usd(nano_usd):
	return Decimal(nano_usd) / NANO


class Reconciler:
	"""One per drained Redis: the price book, the day, and what that Redis had reported before."""

	def __init__(self, book, day, redis, gateway):
		self.book = book
		self.day = day
		self.redis = redis
		self.gateway = gateway
		self.known = spent_known(redis)
		self.on_several_stores = set(
			frappe.get_all("Gateway Spend", filters={"redis": ("!=", redis)}, pluck="grove_user", distinct=True)
		)

	def user(self, user, drains):
		"""`drains`: the parsed hash of each of `user`'s keys in this drain. Prices them, moves
		`spent`, audits the box's figures, then settles the verdict."""
		priced = self.price(drains)
		frappe.db.sql("update `tabGrove User` set spent = spent + %s where name = %s", [sum(priced.values(), Decimal(0)), user])
		requests = sum(drain.requests for drain in drains)
		reported = [drain.money for drain in drains if "user_spent" in drain.money]
		if reported:
			self.audit_prices(priced, drains, requests)
			spent_new, balance = self.audit_counter(user, reported)
		actual = settle(user)
		if reported:
			self.audit_balance(user, balance, actual, requests)
			upsert(
				user, self.redis, spent_known=usd(spent_new), balance_reported=usd(balance),
				gateway=self.gateway, drained_at=frappe.utils.now_datetime(),
			)

	def price(self, drains):
		"""{model: USD} for the deltas at today's rates, over the models Grove knows."""
		per_model = {}
		for drain in drains:
			for model, counts in drain.counters.items():
				if model in self.book.models:
					per_model[model] = per_model.get(model, Decimal(0)) + self.book.usage_cost(counts, model, self.day)
		return per_model

	def audit_prices(self, priced, drains, requests):
		"""The box's `m:cost:<model>` against what Grove priced: beyond one µUSD a request is a
		rate the two sides disagree on — expected for one pull after a price change."""
		drained = {}
		for drain in drains:
			for model, cost in drain.model_cost.items():
				if model in self.book.models:
					drained[model] = drained.get(model, 0) + cost
		for model in set(priced) | set(drained):
			expected, got = priced.get(model, Decimal(0)), usd(drained.get(model, 0))
			if abs(expected - got) > tolerance(requests):
				record(
					"Price Drift", model=model, redis=self.redis, gateway=self.gateway,
					gateway_value=got, expected_value=expected, delta=got - expected,
				)

	def audit_counter(self, user, reported):
		"""→ (the highest lifetime counter reported, the balance that same key reported). The
		counter never falls; below what this Redis last reported means it was flushed."""
		best = max(reported, key=lambda money: money["user_spent"])
		known = self.known.get(user, 0)
		if best["user_spent"] < known:
			record(
				"Counter Reset", grove_user=user, redis=self.redis, gateway=self.gateway,
				gateway_value=usd(best["user_spent"]), expected_value=usd(known), delta=usd(best["user_spent"] - known),
			)
		return best["user_spent"], best.get("user_balance", 0)

	def audit_balance(self, user, balance, actual, requests):
		"""The box believing it has MORE than Grove: a push that never landed. Under is timing (a
		key drained before a later one reported), and a user on several stores always reads over
		by the others' undrained spend, so neither is flagged."""
		reported = usd(balance)
		if user in self.on_several_stores or reported - actual <= tolerance(requests):
			return
		record(
			"Balance Mismatch", grove_user=user, redis=self.redis, gateway=self.gateway,
			gateway_value=reported, expected_value=actual, delta=reported - actual,
		)
