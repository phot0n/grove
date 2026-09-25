# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Prepaid credits end to end on a site: a drain is priced, `spent` moves, the box's own money
figures are audited, the verdict is settled, and the push carries each store its ceiling."""

import unittest.mock
from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase

from grove import pricing
from grove.grove.doctype.grove_user.grove_user import register_user
from grove.pathway import routes, snapshot, usage

NANO = pricing.NANO
D = Decimal


class CreditsCase(IntegrationTestCase):
	"""A model sold at 10 USD/Mtok of completion, so 100 000 tokens cost exactly 1 USD."""

	RATE = 10
	counter = 0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.model = cls.priced_model("credits-7b", cls.RATE)
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with (
			unittest.mock.patch(f"{gateway_module}.sync_fleet_ingress"),
			unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"),
		):
			machine = frappe.get_doc({"doctype": "Machine", "name": "credits-box", "machine_type": "Gateway"}).insert(ignore_permissions=True)
			cls.gateway = frappe.get_doc({"doctype": "Gateway Server", "name": machine.name, "machine": machine.name}).insert(
				ignore_permissions=True, ignore_mandatory=True
			).name

	@classmethod
	def priced_model(cls, model_id, rate):
		model = frappe.get_doc(
			{"doctype": "Model", "model_id": model_id, "modality": "text", "hf_repo": f"org/{model_id}"}
		).insert(ignore_permissions=True).name
		frappe.get_doc({
			"doctype": "Model Pricing", "model": model, "status": "Enabled",
			"rates": [{"counter": "completion_tokens", "rate": rate}],
		}).insert(ignore_permissions=True)
		return model

	def user(self, credit=1, free=0):
		CreditsCase.counter += 1
		doc = frappe.get_doc({
			"doctype": "Grove User", "user": register_user(f"credits-{CreditsCase.counter}@grove.test"), "free": free,
		}).insert(ignore_permissions=True)
		if credit:
			self.credit(doc.name, credit)
		key = frappe.get_doc({"doctype": "Grove API Key", "user": doc.name}).insert(ignore_permissions=True).name
		return doc.name, key

	def credit(self, user, amount, note=None):
		return frappe.get_doc(
			{"doctype": "Grove Credit", "grove_user": user, "amount": amount, "note": note}
		).insert(ignore_permissions=True)

	def balance(self, user):
		return D(str(frappe.db.get_value("Grove User", user, "balance")))

	def hash(self, tokens, cost=None, spent=None, balance=None, model=None):
		"""What one key's drained hash carries: the counters, and the money the gateway wrote."""
		model = model or self.model
		cost = tokens * self.RATE * 1000 if cost is None else cost
		h = {"completion_tokens": tokens, "request_count": 1, f"m:completion_tokens:{model}": tokens,
		     f"m:cost:{model}": cost, "cost": cost}
		if spent is not None:
			h["user_spent"] = spent
			h["user_balance"] = NANO - spent if balance is None else balance
		return h

	def pull(self, usages, redis="store-1"):
		with unittest.mock.patch.object(frappe.db, "commit"):
			return usage.record_usages(self.gateway, usages, redis=redis)

	def state(self, user):
		row = frappe.db.get_value("Grove User", user, ["spent", "rate_limited"], as_dict=True)
		return D(str(row.spent)), row.rate_limited

	def discrepancies(self, **filters):
		return frappe.get_all("Credit Discrepancy", filters={"resolved": 0, **filters}, fields=["kind", "model", "grove_user", "delta"])


class TestADrainIsPricedAndSettled(CreditsCase):
	def test_a_pull_prices_the_delta_moves_spent_and_records_the_stores_counter(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(50_000, spent=500_000_000)})
		self.assertEqual(self.state(user), (D("0.5"), 0))
		spend = frappe.db.get_value("Gateway Spend", {"grove_user": user, "redis": "store-1"}, ["spent_known", "balance_reported", "gateway"], as_dict=True)
		self.assertEqual((D(str(spend.spent_known)), D(str(spend.balance_reported)), spend.gateway), (D("0.5"), D("0.5"), self.gateway))
		self.assertEqual(self.discrepancies(grove_user=user), [])

	def test_spending_the_balance_flags_the_user_and_past_it_is_an_overspend(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(100_000, spent=NANO)})
		self.assertEqual(self.state(user), (D("1"), 1))
		self.assertEqual(self.discrepancies(grove_user=user), [])
		self.pull({key: self.hash(50_000, spent=NANO + 500_000_000)})
		[row] = self.discrepancies(grove_user=user)
		self.assertEqual((row.kind, D(str(row.delta))), ("Overspend", D("-0.5")))
		# A second run updates the one open row rather than piling another.
		self.pull({key: self.hash(10_000, spent=NANO + 600_000_000)})
		self.assertEqual(len(self.discrepancies(grove_user=user)), 1)

	def test_a_top_up_unblocks_and_one_smaller_than_the_debt_does_not(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(150_000, spent=1_500_000_000)})
		self.assertEqual(self.balance(user), D("-0.5"))
		self.credit(user, 0.25)
		self.assertEqual(self.state(user), (D("1.5"), 1))
		self.credit(user, 1)
		self.assertEqual((self.state(user), self.balance(user)), ((D("1.5"), 0), D("0.75")))
		self.assertEqual(self.discrepancies(grove_user=user), [], "the overspend is covered")

	def test_nothing_allocated_blocks_on_save_and_a_free_user_is_never_gated(self):
		blocked, _key = self.user(credit=0)
		self.assertEqual(frappe.db.get_value("Grove User", blocked, "rate_limited"), 1)
		free, key = self.user(credit=0, free=1)
		self.pull({key: self.hash(900_000, spent=9 * NANO)})
		self.assertEqual(self.state(free), (D("9"), 0))

	def test_a_stale_form_keeps_the_databases_spent(self):
		user, key = self.user(credit=1)
		doc = frappe.get_doc("Grove User", user)
		self.pull({key: self.hash(70_000, spent=700_000_000)})
		doc.log_payloads = 1
		doc.save()
		self.assertEqual(self.state(user), (D("0.7"), 0))

	def test_the_ledger_is_append_only_and_refuses_zero_and_unexplained_negatives(self):
		user, _key = self.user(credit=1)
		with self.assertRaises(frappe.ValidationError):
			self.credit(user, 0)
		with self.assertRaises(frappe.ValidationError):
			self.credit(user, -0.5)
		entry = self.credit(user, -0.5, note="refund")
		self.assertEqual(self.balance(user), D("0.5"))
		entry.amount = 5
		with self.assertRaises(frappe.ValidationError):
			entry.save()
		entry.reload()
		with self.assertRaises(frappe.ValidationError):
			entry.delete()


class TestTheAuditOfTheBoxsFigures(CreditsCase):
	def test_a_matching_drain_writes_nothing_and_a_wrong_rate_is_one_price_drift_per_model_and_redis(self):
		one, key_one = self.user(credit=5)
		two, key_two = self.user(credit=5)
		self.pull({key_one: self.hash(50_000, spent=500_000_000)})
		self.assertEqual(self.discrepancies(kind="Price Drift"), [])
		# The box priced at 12 USD/Mtok; Grove at 10. Two users, one row.
		self.pull({key_one: self.hash(50_000, cost=600_000_000, spent=1_100_000_000)})
		self.pull({key_two: self.hash(50_000, cost=600_000_000, spent=600_000_000)})
		rows = self.discrepancies(kind="Price Drift", model=self.model, redis="store-1")
		self.assertEqual([(r.model, D(str(r.delta))) for r in rows], [(self.model, D("0.1"))])
		self.assertEqual(self.state(one), (D("1"), 0), "Grove bills its own price, never the box's")

	def test_a_two_key_drain_whose_counter_runs_ahead_of_its_cost_is_not_drift(self):
		user, key_a = self.user(credit=5)
		key_b = frappe.get_doc({"doctype": "Grove API Key", "user": user}).insert(ignore_permissions=True).name
		self.pull({key_a: self.hash(10_000, spent=100_000_000), key_b: self.hash(10_000, spent=300_000_000)})
		self.assertEqual(self.discrepancies(grove_user=user), [])
		self.assertEqual(D(str(frappe.db.get_value("Gateway Spend", {"grove_user": user, "redis": "store-1"}, "spent_known"))), D("0.3"))

	def test_a_counter_below_the_known_high_is_a_counter_reset_and_becomes_the_new_base(self):
		user, key = self.user(credit=5)
		self.pull({key: self.hash(50_000, spent=500_000_000)})
		self.pull({key: self.hash(10_000, spent=100_000_000)})
		[row] = self.discrepancies(grove_user=user)
		self.assertEqual(row.kind, "Counter Reset")
		self.assertEqual(D(str(frappe.db.get_value("Gateway Spend", {"grove_user": user, "redis": "store-1"}, "spent_known"))), D("0.1"))

	def test_a_box_believing_it_has_more_is_a_balance_mismatch_and_under_is_timing(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(50_000, spent=500_000_000, balance=200_000_000)})
		self.assertEqual(self.discrepancies(grove_user=user), [])
		self.pull({key: self.hash(10_000, spent=600_000_000, balance=900_000_000)})
		[row] = self.discrepancies(grove_user=user)
		self.assertEqual((row.kind, D(str(row.delta))), ("Balance Mismatch", D("0.5")))

	def test_an_old_gateways_drain_prices_and_settles_but_audits_nothing(self):
		user, key = self.user(credit=1)
		self.pull({key: {"completion_tokens": 100_000, "request_count": 1, f"m:completion_tokens:{self.model}": 100_000}})
		self.assertEqual(self.state(user), (D("1"), 1))
		self.assertFalse(frappe.db.exists("Gateway Spend", {"grove_user": user}))


class TestTheRebuild(CreditsCase):
	def test_a_blocked_user_the_reprice_funds_is_cleared_without_a_drain(self):
		model = self.priced_model("credits-reprice-7b", 10)
		user, key = self.user(credit=1)
		self.pull({key: self.hash(100_000, spent=NANO, model=model)})
		self.assertEqual(self.state(user), (D("1"), 1))
		frappe.get_doc({
			"doctype": "Model Pricing", "model": model, "status": "Enabled", "rates": [{"counter": "completion_tokens", "rate": 5}],
		}).insert(ignore_permissions=True)
		with unittest.mock.patch.object(frappe.db, "commit"):
			pricing.verify_balances()
		self.assertEqual(self.state(user), (D("0.5"), 0))

	def test_ledger_drift_is_logged_then_the_join_wins(self):
		user, key = self.user(credit=5)
		self.pull({key: self.hash(50_000, spent=500_000_000)})
		frappe.db.set_value("Grove User", user, "spent", 3)
		with unittest.mock.patch.object(frappe.db, "commit"):
			pricing.verify_balances()
		[row] = self.discrepancies(grove_user=user)
		self.assertEqual((row.kind, D(str(row.delta))), ("Ledger Drift", D("-2.5")))
		self.assertEqual(self.state(user), (D("0.5"), 0))


class TestThePushCarriesTheCeiling(CreditsCase):
	def users(self, redis):
		with unittest.mock.patch.object(snapshot, "effective_groups", return_value=[]), unittest.mock.patch.object(snapshot, "effective_keys", return_value=[]):
			shared = {}
			section = snapshot.gateway_snapshot("", redis, shared)["users"]
		return {u["name"]: u for bucket in section["buckets"].values() for u in bucket["records"]}

	def test_each_store_is_pushed_what_is_left_plus_what_it_already_counted(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(30_000, spent=300_000_000)}, redis="store-1")
		self.pull({key: self.hash(20_000, spent=200_000_000)}, redis="store-2")
		self.assertEqual(self.state(user), (D("0.5"), 0))
		self.assertEqual(self.users("store-1")[user]["budget"], 800_000_000)
		self.assertEqual(self.users("store-2")[user]["budget"], 700_000_000)
		self.assertEqual(self.users("store-3")[user]["budget"], 500_000_000)
		self.assertIs(self.users("store-1")[user]["prepaid"], True)

	def test_combined_spend_past_the_balance_is_one_overspend_and_blocks_everywhere(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(80_000, spent=800_000_000)}, redis="store-1")
		self.pull({key: self.hash(70_000, spent=700_000_000)}, redis="store-2")
		self.assertEqual(self.state(user), (D("1.5"), 1))
		self.assertEqual([r.kind for r in self.discrepancies(grove_user=user)], ["Overspend"])
		self.assertEqual(self.users("store-1")[user]["budget"], 300_000_000)

	def test_a_sub_micro_usd_move_in_the_balance_leaves_the_record_alone(self):
		user, _key = self.user(credit=1)
		frappe.db.set_value("Grove User", user, "balance", 0.5)
		before = self.users("store-1")[user]
		frappe.db.set_value("Grove User", user, "balance", 0.4999996)
		self.assertEqual(self.users("store-1")[user], before)

	def test_routes_carry_todays_rates_per_counter_in_nano_usd(self):
		table = {self.model: [{"deployment": "d1"}, {"deployment": "d2"}], "unpriced/model": [{"deployment": "d3"}]}
		routes.add_rates(table)
		self.assertEqual([row["rates"] for row in table[self.model]], [{"completion_tokens": 10 * NANO}] * 2)
		self.assertNotIn("rates", table["unpriced/model"][0])
