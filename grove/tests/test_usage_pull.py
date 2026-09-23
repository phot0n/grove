# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The usage pull ADDs deltas into monthly Usage Records. A key with traffic used to cost a full
document save per pull — load, child-table diff, modified bump, and a budget sum in on_update —
so n keys were n saves. Now rows are incremented in place, only what is missing is created, and
the budget is checked once per user after the loop."""

import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase

from grove import usage_pull
from grove.grove.doctype.grove_user.grove_user import register_user
from grove.grove.doctype.usage_record.usage_record import current_month, enforce_budget

FIELDS = usage_pull._FIELDS


def delta(n):
	return {f: n for f in FIELDS}


class TestAPullAddsInPlace(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.month = current_month()
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("usage-pull@grove.test")}
		).insert(ignore_permissions=True).name
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "usage-pull-7b", "modality": "text", "hf_repo": "org/usage-pull-7b"}
		).insert(ignore_permissions=True).name
		# Two front boxes. The fleet hooks reach for DNS and security groups, which no test wants.
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with (
			unittest.mock.patch(f"{gateway_module}.sync_fleet_ingress"),
			unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"),
		):
			cls.gateways = []
			for i in (1, 2):
				machine = frappe.get_doc({
					"doctype": "Machine", "name": f"usage-pull-box-{i}", "machine_type": "Gateway",
				}).insert(ignore_permissions=True)
				gateway = frappe.get_doc({"doctype": "Gateway Server", "machine": machine.name}).insert(
					ignore_permissions=True, ignore_mandatory=True
				)
				cls.gateways.append(gateway.name)

	def key(self):
		return frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name

	def record(self, key):
		name = frappe.db.exists("Usage Record", {"month": self.month, "api_key": key})
		return frappe.get_doc("Usage Record", name) if name else None

	def test_the_first_pull_creates_the_record_and_its_rows(self):
		key = self.key()
		usage_pull._add_delta(self.gateways[0], key, self.user, self.month, delta(10), {self.model: delta(10)})
		doc = self.record(key)
		self.assertEqual(doc.user, self.user)
		self.assertEqual(doc.prompt_tokens, 10)
		self.assertEqual([(r.gateway_server, r.prompt_tokens) for r in doc.gateway_usage], [(self.gateways[0], 10)])
		self.assertEqual([(r.model, r.prompt_tokens) for r in doc.model_usage], [(self.model, 10)])

	def test_the_second_pull_increments_without_saving_the_document(self):
		key = self.key()
		usage_pull._add_delta(self.gateways[0], key, self.user, self.month, delta(10), {self.model: delta(10)})
		before = self.record(key)
		usage_pull._add_delta(self.gateways[0], key, self.user, self.month, delta(5), {self.model: delta(5)})
		after = self.record(key)
		self.assertEqual(after.prompt_tokens, 15)
		self.assertEqual(after.gateway_usage[0].prompt_tokens, 15)
		self.assertEqual(after.model_usage[0].prompt_tokens, 15)
		# The UPDATE stamps modified itself, so the list view shows when usage last landed.
		self.assertGreater(after.modified, before.modified)
		self.assertNotEqual(before.gateway_usage[0].last_pulled, after.gateway_usage[0].last_pulled)

	def test_the_totals_are_the_sum_of_the_gateway_rows(self):
		key = self.key()
		usage_pull._add_delta(self.gateways[0], key, self.user, self.month, delta(10))
		usage_pull._add_delta(self.gateways[1], key, self.user, self.month, delta(7))
		usage_pull._add_delta(self.gateways[0], key, self.user, self.month, delta(1))
		doc = self.record(key)
		self.assertEqual(sorted((r.gateway_server, r.prompt_tokens) for r in doc.gateway_usage),
		                 sorted([(self.gateways[0], 11), (self.gateways[1], 7)]))
		self.assertEqual(doc.prompt_tokens, sum(r.prompt_tokens for r in doc.gateway_usage))

	def test_a_model_that_no_longer_exists_keeps_its_tokens_in_the_totals(self):
		key = self.key()
		usage_pull._add_delta(self.gateways[0], key, self.user, self.month, delta(10),
		                      {self.model: delta(4), "frappe/retired-model": delta(6)})
		doc = self.record(key)
		self.assertEqual(doc.prompt_tokens, 10)
		self.assertEqual([(r.model, r.prompt_tokens) for r in doc.model_usage], [(self.model, 4)])


class TestTheBudgetIsCheckedOncePerUser(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.month = current_month()
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("usage-budget@grove.test"), "max_tokens": 100}
		).insert(ignore_permissions=True).name

	def test_reaching_the_budget_flags_the_user_once(self):
		key = frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Usage Record", "api_key": key, "user": self.user, "month": self.month,
		                "prompt_tokens": 150, "cached_tokens": 0}).insert(ignore_permissions=True)
		self.assertTrue(enforce_budget(self.user, self.month))
		self.assertEqual(frappe.db.get_value("Grove User", self.user, "rate_limited"), 1)
		self.assertFalse(enforce_budget(self.user, self.month), "already flagged: nothing changed")

	def test_a_pull_checks_each_touched_user_once_not_per_key(self):
		keys = [frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name
		        for _ in range(3)]
		response = unittest.mock.Mock()
		response.json.return_value = {"usages": {k: {"prompt_tokens": 1, "request_count": 1} for k in keys}}
		gateway = unittest.mock.Mock(admin_url="http://gw.test", get_password=lambda *a, **k: "tok")
		with (
			unittest.mock.patch("grove.usage_pull.requests.get", return_value=response),
			unittest.mock.patch("grove.usage_pull.frappe.get_doc", return_value=gateway),
			unittest.mock.patch("grove.usage_pull._add_delta") as add_delta,
			unittest.mock.patch("grove.usage_pull.enforce_budget") as budget,
			unittest.mock.patch("grove.usage_pull.frappe.db.commit"),
		):
			self.assertEqual(usage_pull._pull_proxy("gw-1"), (3, 0))
		self.assertEqual(add_delta.call_count, 3)
		budget.assert_called_once_with(self.user, self.month)


class TestOneKeysFailureDoesNotLoseTheOthers(IntegrationTestCase):
	"""The gateway deletes every counter as it hands them over, so a pull that dies on key 900
	used to lose keys 1–899 with it. Each key is its own savepoint now, and a delta that cannot be
	recorded goes to the Error Log with the drained payload, so it can be replayed by hand."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.month = current_month()
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("usage-lost@grove.test")}
		).insert(ignore_permissions=True).name
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with (
			unittest.mock.patch(f"{gateway_module}.sync_fleet_ingress"),
			unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"),
		):
			machine = frappe.get_doc({
				"doctype": "Machine", "name": "usage-lost-box", "machine_type": "Gateway",
			}).insert(ignore_permissions=True)
			cls.gateway = frappe.get_doc({"doctype": "Gateway Server", "machine": machine.name}).insert(
				ignore_permissions=True, ignore_mandatory=True
			).name

	def keys(self, n):
		return [frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name
		        for _ in range(n)]

	def pull(self, usages):
		response = unittest.mock.Mock()
		response.json.return_value = {"usages": usages}
		gateway = unittest.mock.Mock(admin_url="http://gw.test", get_password=lambda *a, **k: "tok")
		real_get_doc = frappe.get_doc

		# Only the box lookup is faked; the records themselves are real — frappe.new_doc goes
		# through get_doc too, so a blanket patch would break the very path under test.
		def get_doc(*args, **kwargs):
			return gateway if args and args[0] == "Gateway Server" else real_get_doc(*args, **kwargs)

		with (
			unittest.mock.patch("grove.usage_pull.requests.get", return_value=response),
			unittest.mock.patch("grove.usage_pull.frappe.get_doc", side_effect=get_doc),
			unittest.mock.patch("grove.usage_pull.frappe.db.commit"),
		):
			return usage_pull._pull_proxy(self.gateway)

	def test_the_failed_key_is_rolled_back_and_logged_and_the_rest_land(self):
		bad, good = self.keys(2)
		real = usage_pull._increment

		def increment(doctype, amounts, now, **where):
			if where.get("parent") == frappe.db.exists("Usage Record", {"month": self.month, "api_key": bad}):
				raise RuntimeError("disk on fire")
			return real(doctype, amounts, now, **where)

		usage = {"prompt_tokens": 9, "request_count": 1, "m:prompt_tokens:frappe/x": 9}
		with (
			unittest.mock.patch("grove.usage_pull._increment", side_effect=increment),
			unittest.mock.patch("grove.usage_pull.frappe.log_error") as log_error,
		):
			self.assertEqual(self.pull({bad: usage, good: usage}), (2, 1))

		# The bad key's record was created on the way to the failure; the savepoint took it back.
		self.assertFalse(frappe.db.exists("Usage Record", {"month": self.month, "api_key": bad}))
		self.assertEqual(frappe.db.get_value("Usage Record", {"month": self.month, "api_key": good}, "prompt_tokens"), 9)
		log_error.assert_called_once()
		title, message = log_error.call_args.kwargs["title"], log_error.call_args.kwargs["message"]
		self.assertIn(bad, title)
		self.assertIn("disk on fire", message)
		for needle in (f'"api_key": "{bad}"', f'"gateway_server": "{self.gateway}"', '"m:prompt_tokens:frappe/x": 9'):
			with self.subTest(needle):
				self.assertIn(needle, message)

	def test_the_sync_row_says_how_many_were_lost(self):
		with unittest.mock.patch("grove.usage_pull._pull_proxy", return_value=(3, 1)):
			self.assertEqual(usage_pull._pull_and_classify("gw-1")["detail"], "pulled:3 lost:1")
		with unittest.mock.patch("grove.usage_pull._pull_proxy", return_value=(3, 0)):
			self.assertEqual(usage_pull._pull_and_classify("gw-1")["detail"], "pulled:3")


class TestAStoreIsPulledThroughOneWriter(unittest.TestCase):
	"""Every gateway on a store shares its counters, so the pull drains a store once, through the
	first of its writers that answers. Pure: the run doc and the pull are fakes."""

	def pull(self, groups, succeeds, **kwargs):
		doc, pulled = unittest.mock.MagicMock(), []
		doc.acquire_lock.return_value = True
		doc.results = []
		doc.append.side_effect = lambda _field, row: doc.results.append(row)

		def pull_one(gateway):
			pulled.append(gateway)
			return {"success": int(gateway in succeeds), "reachable": 1}

		with (
			unittest.mock.patch.object(usage_pull, "_new_run", return_value=doc),
			unittest.mock.patch.object(usage_pull, "sync_targets", return_value=groups),
			unittest.mock.patch.object(usage_pull, "_pull_and_classify", side_effect=pull_one),
			unittest.mock.patch.object(usage_pull, "_finalize") as finalize,
			unittest.mock.patch.object(frappe, "db", frappe._dict(commit=lambda: None)),
		):
			usage_pull.pull_all(**kwargs)
		return pulled, finalize.call_args.args[1:]

	def test_a_named_gateway_is_pulled_itself_whatever_store_it_is_on(self):
		pulled, counts = self.pull([("store1", ["gw1"])], succeeds={"gw9"}, gateways=["gw9"])
		self.assertEqual((pulled, counts), (["gw9"], (1, 1)))

	def test_the_next_writer_is_tried_when_one_fails_and_none_after_a_success(self):
		pulled, counts = self.pull([(None, ["gw0"]), ("store1", ["gw1", "gw2", "gw3"])], succeeds={"gw0", "gw2"})
		self.assertEqual(pulled, ["gw0", "gw1", "gw2"])
		self.assertEqual(counts, (2, 2))

	def test_a_store_with_no_writer_is_a_failed_target(self):
		pulled, counts = self.pull([("store1", [])], succeeds=set())
		self.assertEqual((pulled, counts), ([], (1, 0)))
