# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The usage pull ADDs deltas into UTC-day Usage Records. A key with traffic used to cost a full
document save per pull — load, child-table diff, modified bump, and a budget sum in on_update —
so n keys were n saves. Now rows are incremented in place, only what is missing is created, and
each touched user is reconciled once after the loop."""

import threading
import unittest.mock
from datetime import timedelta

import frappe
from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.lost_usage import lost_usage
from grove.pathway import run, usage
from grove.pathway.run import Target
from grove.grove.doctype.grove_user.grove_user import register_user
from grove.utils import utc_today

class TestAPullAddsInPlace(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.day = utc_today()
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
				gateway = frappe.get_doc({"doctype": "Gateway Server", "name": machine.name, "machine": machine.name}).insert(
					ignore_permissions=True, ignore_mandatory=True
				)
				cls.gateways.append(gateway.name)

	def key(self):
		return frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name

	def record(self, key):
		name = frappe.db.exists("Usage Record", {"day": self.day, "api_key": key})
		return frappe.get_doc("Usage Record", name) if name else None

	def test_the_first_pull_creates_the_record_and_its_rows(self):
		key = self.key()
		usage.add_delta(self.gateways[0], key, self.user, self.day, 1, {self.model: {"completion_tokens": 10}})
		doc = self.record(key)
		self.assertEqual((doc.user, doc.day), (self.user, self.day))
		self.assertEqual([(r.redis, r.request_count) for r in doc.gateway_usage], [(self.gateways[0], 1)])
		self.assertEqual([(r.model, r.counter, r.amount) for r in doc.counter_usage], [(self.model, "completion_tokens", 10)])

	def test_the_second_pull_increments_without_saving_the_document(self):
		key = self.key()
		usage.add_delta(self.gateways[0], key, self.user, self.day, 1, {self.model: {"completion_tokens": 10}})
		before = self.record(key)
		usage.add_delta(self.gateways[0], key, self.user, self.day, 2, {self.model: {"completion_tokens": 5, "cached_tokens": 2}})
		after = self.record(key)
		self.assertEqual(after.gateway_usage[0].request_count, 3)
		self.assertEqual({(r.counter, r.amount) for r in after.counter_usage}, {("completion_tokens", 15), ("cached_tokens", 2)})
		# The UPDATE stamps modified itself, so the list view shows when usage last landed.
		self.assertGreater(after.modified, before.modified)
		self.assertNotEqual(before.gateway_usage[0].last_pulled, after.gateway_usage[0].last_pulled)

	def test_requests_are_counted_per_redis(self):
		key = self.key()
		usage.add_delta("store-a", key, self.user, self.day, 10)
		usage.add_delta(self.gateways[1], key, self.user, self.day, 7)
		usage.add_delta("store-a", key, self.user, self.day, 1)
		doc = self.record(key)
		self.assertEqual(sorted((r.redis, r.request_count) for r in doc.gateway_usage),
		                 sorted([("store-a", 11), (self.gateways[1], 7)]))

	def test_a_name_grove_does_not_know_gets_no_row_and_does_not_fail_the_pull(self):
		key = self.key()
		usage.add_delta(self.gateways[0], key, self.user, self.day, 1,
		                      {self.model: {"completion_tokens": 4}, "MD-00007": {"completion_tokens": 6}})
		doc = self.record(key)
		self.assertEqual([(r.model, r.amount) for r in doc.counter_usage], [(self.model, 4)])

	def test_an_unpublished_models_usage_still_lands(self):
		key = self.key()
		frappe.db.set_value("Model", self.model, "published", 0)
		self.addCleanup(frappe.db.set_value, "Model", self.model, "published", 1)
		usage.add_delta(self.gateways[0], key, self.user, self.day, 1, {self.model: {"completion_tokens": 4}})
		self.assertEqual([(r.model, r.amount) for r in self.record(key).counter_usage], [(self.model, 4)])


class TestEachTouchedUserIsReconciledOnce(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("usage-reconcile@grove.test")}
		).insert(ignore_permissions=True).name

	def test_a_pull_reconciles_each_touched_user_once_not_per_key(self):
		keys = [frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name
		        for _ in range(3)]
		usages = {k: {"prompt_tokens": 1, "request_count": 1} for k in keys}
		with (
			unittest.mock.patch("grove.pathway.usage.add_delta") as add_delta,
			unittest.mock.patch("grove.pathway.usage.Reconciler") as reconciler,
			unittest.mock.patch("grove.pathway.usage.frappe.db.commit"),
		):
			self.assertEqual(usage.record_usages("gw-1", usages, redis="store-1"), (3, 0))
		self.assertEqual(add_delta.call_count, 3)
		reconciler.return_value.user.assert_called_once()
		user, drains = reconciler.return_value.user.call_args.args
		self.assertEqual((user, len(drains)), (self.user, 3))
		self.assertEqual(reconciler.call_args.args[2:], ("store-1", "gw-1"))

	def test_one_users_failure_skips_nobody(self):
		other = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("usage-reconcile-2@grove.test")}
		).insert(ignore_permissions=True).name
		keys = {
			frappe.get_doc({"doctype": "Grove API Key", "user": u}).insert(ignore_permissions=True).name: u
			for u in (self.user, other)
		}
		seen = []

		def reconcile(user, drains):
			seen.append(user)
			if user == self.user:
				raise RuntimeError("bug in the reconcile")

		with (
			unittest.mock.patch("grove.pathway.usage.add_delta"),
			unittest.mock.patch("grove.pathway.usage.Reconciler") as reconciler,
			unittest.mock.patch("grove.pathway.usage.frappe.db.commit"),
			unittest.mock.patch("grove.pathway.usage.frappe.log_error") as log_error,
		):
			reconciler.return_value.user.side_effect = reconcile
			usage.record_usages("gw-1", {k: {"request_count": 1} for k in keys})
		self.assertEqual(sorted(seen), sorted(keys.values()))
		log_error.assert_called_once()
		self.assertIn(self.user, log_error.call_args.kwargs["title"])


class TestOneKeysFailureDoesNotLoseTheOthers(IntegrationTestCase):
	"""The gateway deletes every counter as it hands them over, so a pull that dies on key 900
	used to lose keys 1–899 with it. Each key is its own savepoint now, and a delta that cannot be
	recorded goes to the Error Log with the drained payload, so it can be replayed by hand."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.day = utc_today()
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
			cls.gateway = frappe.get_doc({"doctype": "Gateway Server", "name": machine.name, "machine": machine.name}).insert(
				ignore_permissions=True, ignore_mandatory=True
			).name

	def keys(self, n):
		return [frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name
		        for _ in range(n)]

	def pull(self, usages):
		with unittest.mock.patch("grove.pathway.usage.frappe.db.commit"):
			return usage.record_usages(self.gateway, usages)

	def test_a_named_gateways_drain_lands_under_its_store(self):
		# The operator's button dials one box with no store on its unit; the boxes on a store share
		# their counters, so what it drained is the store's — the row, the reconciler, Gateway Spend.
		[key] = self.keys(1)
		with (
			unittest.mock.patch("grove.pathway.usage.snapshot.gateway_redis", return_value="store-9"),
			unittest.mock.patch("grove.pathway.usage.Reconciler") as reconciler,
		):
			self.pull({key: {"request_count": 1}})
		record = frappe.db.exists("Usage Record", {"day": self.day, "api_key": key})
		self.assertEqual(frappe.get_all("Usage Gateway Row", filters={"parent": record}, pluck="redis", parent_doctype="Usage Record"), ["store-9"])
		self.assertEqual(reconciler.call_args.args[2:], ("store-9", self.gateway))

	def test_the_failed_key_is_rolled_back_and_logged_and_the_rest_land(self):
		bad, good = self.keys(2)
		real = usage.ensure_rows

		def ensure_rows(prefix, *args):
			# The record is created first, so the failure lands after a write the savepoint must undo.
			name = real(prefix, *args)
			if prefix == bad:
				raise RuntimeError("disk on fire")
			return name

		drained = {"prompt_tokens": 9, "request_count": 1, "m:prompt_tokens:frappe/x": 9}
		with unittest.mock.patch("grove.pathway.usage.ensure_rows", side_effect=ensure_rows):
			self.assertEqual(self.pull({bad: drained, good: drained}), (2, 1))

		# The bad key's record was created on the way to the failure; the savepoint took it back.
		self.assertFalse(frappe.db.exists("Usage Record", {"day": self.day, "api_key": bad}))
		good_record = frappe.db.exists("Usage Record", {"day": self.day, "api_key": good})
		self.assertEqual(
			frappe.db.get_value("Usage Gateway Row", {"parent": good_record, "redis": self.gateway}, "request_count"), 1
		)
		# The bad key's payload is a Lost Usage row, verbatim, with the reason.
		lost = frappe.get_doc("Lost Usage", {"api_key": bad})
		self.assertEqual((lost.gateway_server, lost.grove_user, str(lost.day)), (self.gateway, self.user, str(self.day)))
		self.assertEqual(frappe.parse_json(lost.payload), {bad: drained})
		self.assertIn("disk on fire", lost.last_error)

	def test_the_sync_row_says_how_many_were_lost(self):
		def detail(counts):
			row = {"server": "gw-1", "success": 1, "duration_ms": 0, "usages": {"k": {}}}
			with unittest.mock.patch("grove.pathway.usage.record_usages", return_value=counts):
				return usage.record_drain(True, [row])[1][0]["detail"]

		self.assertEqual(detail((3, 1)), "pulled:3 lost:1")
		self.assertEqual(detail((3, 0)), "pulled:3")


class TestALostDrainIsReplayedOnce(IntegrationTestCase):
	"""What a failed pull could not record is a Lost Usage row; the hourly replay lands it on the
	day it was drained, once, and a replay that fails again stays pending with its error."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.day = utc_today()
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("usage-replay@grove.test")}
		).insert(ignore_permissions=True).name
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with (
			unittest.mock.patch(f"{gateway_module}.sync_fleet_ingress"),
			unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"),
		):
			machine = frappe.get_doc({"doctype": "Machine", "name": "usage-replay-box", "machine_type": "Gateway"}).insert(ignore_permissions=True)
			cls.gateway = frappe.get_doc({"doctype": "Gateway Server", "name": machine.name, "machine": machine.name}).insert(
				ignore_permissions=True, ignore_mandatory=True
			).name

	def key(self):
		return frappe.get_doc({"doctype": "Grove API Key", "user": self.user}).insert(ignore_permissions=True).name

	def requests_for(self, key, day=None):
		record = frappe.db.exists("Usage Record", {"day": day or self.day, "api_key": key})
		return record and frappe.db.get_value("Usage Gateway Row", {"parent": record, "redis": self.gateway}, "request_count")

	def lost_for(self, key):
		return frappe.get_doc("Lost Usage", {"payload": ("like", f"%{key}%")})

	def replay(self):
		with unittest.mock.patch.object(frappe.db, "commit"):
			return lost_usage.replay_pending()

	def test_a_lost_delta_lands_once(self):
		key = self.key()
		with (
			unittest.mock.patch("grove.pathway.usage.ensure_rows", side_effect=RuntimeError("disk on fire")),
			unittest.mock.patch.object(frappe.db, "commit"),
		):
			self.assertEqual(usage.record_usages(self.gateway, {key: {"request_count": 2}}), (1, 1))
		self.assertIsNone(self.requests_for(key))
		lost = self.lost_for(key)
		self.assertEqual((lost.api_key, lost.grove_user, lost.replayed), (key, self.user, 0))
		self.assertIn("disk on fire", lost.last_error)

		first, again = self.replay(), self.replay()
		self.assertEqual(self.requests_for(key), 2)
		self.assertIn(lost.name, first)
		self.assertNotIn(lost.name, again)
		lost.reload()
		self.assertEqual((lost.replayed, lost.attempts), (1, 1))

	def test_a_lost_drain_lands_on_the_day_it_was_drained(self):
		key = self.key()
		rows = [{"server": self.gateway, "success": 1, "usages": {key: {"request_count": 3}}, "duration_ms": 0}]
		with (
			unittest.mock.patch("grove.pathway.usage.record_usages", side_effect=RuntimeError("db gone")),
			unittest.mock.patch.object(frappe.db, "commit"),
			unittest.mock.patch.object(frappe.db, "rollback"),
		):
			outcome, _ = usage.record_drain(True, rows)
		self.assertFalse(outcome)
		lost = self.lost_for(key)
		self.assertEqual(lost.api_key, None)
		yesterday = self.day - timedelta(days=1)
		frappe.db.set_value("Lost Usage", lost.name, "day", yesterday)

		self.assertIn(lost.name, self.replay())
		self.assertEqual(self.requests_for(key, day=yesterday), 3)
		self.assertIsNone(self.requests_for(key))

	def test_a_replay_that_fails_again_stays_pending_with_its_error(self):
		key = self.key()
		with (
			unittest.mock.patch("grove.pathway.usage.ensure_rows", side_effect=RuntimeError("disk on fire")),
			unittest.mock.patch.object(frappe.db, "commit"),
		):
			usage.record_usages(self.gateway, {key: {"request_count": 1}})
		lost = self.lost_for(key)
		with (
			unittest.mock.patch("grove.pathway.usage.record_usages", side_effect=RuntimeError("still down")),
			unittest.mock.patch.object(frappe.db, "rollback"),
		):
			self.assertNotIn(lost.name, self.replay())
		lost.reload()
		self.assertEqual((lost.replayed, lost.attempts), (0, 1))
		self.assertIn("still down", lost.last_error)

	def test_log_settings_clears_old_replayed_rows_and_keeps_pending_ones(self):
		landed, pending = self.key(), self.key()
		long_ago = frappe.utils.add_days(frappe.utils.now_datetime(), -100)
		with (
			unittest.mock.patch("grove.pathway.usage.ensure_rows", side_effect=RuntimeError("down")),
			unittest.mock.patch.object(frappe.db, "commit"),
		):
			for key in (landed, pending):
				usage.record_usages(self.gateway, {key: {"request_count": 1}})
		landed_row, pending_row = self.lost_for(landed), self.lost_for(pending)
		landed_row.db_set({"replayed": 1, "replayed_on": long_ago})
		pending_row.db_set({"creation": long_ago})

		self.assertTrue(_supports_log_clearing("Lost Usage"))
		lost_usage.LostUsage.clear_old_logs(days=90)
		self.assertFalse(frappe.db.exists("Lost Usage", landed_row.name))
		self.assertTrue(frappe.db.exists("Lost Usage", pending_row.name))


class TestAStoreIsPulledThroughOneWriter(unittest.TestCase):
	"""Every gateway on a store shares its counters, so the pull drains a store once, through the
	first of its writers that answers. Pure: the run doc and the pull are fakes."""

	def pull(self, groups, succeeds, record=None, fetch=None, **kwargs):
		doc, pulled = unittest.mock.MagicMock(), []
		doc.acquire_lock.return_value = True
		doc.results = []
		doc.append.side_effect = lambda _field, row: doc.results.append(row)
		self.doc = doc

		def fetch_one(target):
			pulled.append(target.name)
			return {"success": int(target.name in succeeds), "reachable": 1, "duration_ms": 0,
			        "usages": {"key": {"request_count": 1}}}

		with (
			unittest.mock.patch.object(run, "new_run", return_value=doc),
			unittest.mock.patch.object(run, "sync_targets", return_value=groups),
			unittest.mock.patch.object(
				Target, "resolve", side_effect=lambda kind, name: Target(kind, name, "http://x", "t")
			),
			unittest.mock.patch.object(usage, "fetch_usage", side_effect=fetch or fetch_one),
			unittest.mock.patch.object(usage, "record_usages", side_effect=record or (lambda *_, **__: (1, 0))),
			unittest.mock.patch.object(usage, "record_lost") as self.lost,
			unittest.mock.patch.object(run, "finalize") as finalize,
			unittest.mock.patch.object(frappe, "db", frappe._dict(commit=lambda: None, rollback=lambda: None)),
		):
			usage.pull_all(**kwargs)
		return pulled, finalize.call_args.args[1:]

	def test_a_named_gateway_is_pulled_itself_whatever_store_it_is_on(self):
		pulled, counts = self.pull([("store1", ["gw1"])], succeeds={"gw9"}, gateways=["gw9"])
		self.assertEqual((pulled, counts), (["gw9"], (1, 1)))

	def test_the_next_writer_is_tried_when_one_fails_and_none_after_a_success(self):
		pulled, counts = self.pull([(None, ["gw0"]), ("store1", ["gw1", "gw2", "gw3"])], succeeds={"gw0", "gw2"})
		# The two groups run side by side; only a store's own writers are ordered.
		self.assertEqual(sorted(pulled), ["gw0", "gw1", "gw2"])
		self.assertLess(pulled.index("gw1"), pulled.index("gw2"))
		self.assertEqual(counts, (2, 2))

	def test_a_store_with_no_writer_is_a_failed_target(self):
		pulled, counts = self.pull([("store1", [])], succeeds=set())
		self.assertEqual((pulled, counts), ([], (1, 0)))

	def test_rows_land_in_the_order_asked_whichever_store_answers_first(self):
		first_may_answer = threading.Event()

		def fetch(target):
			if target.name == "gw1":
				first_may_answer.wait(5)
			else:
				first_may_answer.set()
			return {"success": 1, "reachable": 1, "duration_ms": 0, "usages": {}}

		self.pull([(None, ["gw1"]), (None, ["gw2"])], succeeds=set(), fetch=fetch)
		self.assertEqual([row["server"] for row in self.doc.results], ["gw1", "gw2"])

	def test_a_drain_that_cannot_be_recorded_fails_its_own_row_and_keeps_the_payload(self):
		def record(gateway, _usages, **_):
			if gateway == "gw1":
				raise RuntimeError("disk on fire")
			return 4, 0

		_pulled, counts = self.pull([(None, ["gw1"]), (None, ["gw2"])], succeeds={"gw1", "gw2"}, record=record)
		bad, good = self.doc.results
		self.assertEqual(counts, (2, 1))
		self.assertEqual((bad["success"], good["success"]), (0, 1))
		self.assertIn("disk on fire", bad["error"])
		self.assertEqual(good["detail"], "pulled:4")
		self.lost.assert_called_once()
		# A box on its own Redis: no store on the unit, resolved at replay.
		gateway, redis, _day, usages = self.lost.call_args.args
		self.assertEqual((gateway, redis, usages), ("gw1", None, {"key": {"request_count": 1}}))

	def test_the_drained_hashes_never_reach_the_row(self):
		self.pull([(None, ["gw1"])], succeeds={"gw1"})
		self.assertNotIn("usages", self.doc.results[0])


class TestFetchUsageNeedsNoFrappe(unittest.TestCase):
	"""It runs on a pool thread, where there is no frappe.local to reach for."""

	def on_a_bare_thread(self, work):
		box = {}
		thread = threading.Thread(target=lambda: box.update(result=work()))
		thread.start()
		thread.join(5)
		return box["result"]

	def test_a_drain_comes_back_with_its_hashes(self):
		response = unittest.mock.Mock()
		response.json.return_value = {"usages": {"k": {"request_count": 2}}}
		with unittest.mock.patch("grove.pathway.run.requests.get", return_value=response):
			row = self.on_a_bare_thread(lambda: usage.fetch_usage(Target("Gateway Server", "gw", "http://x", "t")))
		self.assertEqual((row["success"], row["usages"]), (1, {"k": {"request_count": 2}}))

	def test_a_box_that_could_not_be_resolved_is_a_failed_row_not_a_call(self):
		with unittest.mock.patch("grove.pathway.run.requests.get") as get:
			row = self.on_a_bare_thread(
				lambda: usage.fetch_usage(Target("Gateway Server", "gw", error="no admin_url"))
			)
		get.assert_not_called()
		self.assertEqual((row["success"], row["error"]), (0, "no admin_url"))
