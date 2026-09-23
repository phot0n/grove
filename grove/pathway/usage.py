# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Pull usage from each gateway Redis into UTC-day Usage Records, and reconcile the money.

The gateway accumulates per-key deltas in `usage:<prefix>`. A pull GETs /usage, which atomically
reads-and-deletes each live counter (HGETALL + DEL in one Lua call), stamps the day from OUR clock,
and ADDs the delta into the (key, day) record. Then, once per touched user, the deltas are priced,
`spent` moves, the box's own money figures are audited and the verdict is settled.

**1-shot, no retry**: the drain deletes the counter as it returns it, so a crash between the
response and the commit loses that cycle's delta. Never double-count, rare bounded loss on failure.
Requests metered mid-pull land either fully in the snapshot or on the fresh key, never split.

Gateway Redises are drained in parallel and each drain is recorded the moment it arrives, on the
main thread. A drain may wait behind another store's write, so that window is slightly wider."""

import time

import frappe

from grove.grove.doctype.lost_usage.lost_usage import record_lost
from grove.pathway import snapshot
from grove.pathway.reconcile import Reconciler
from grove.pathway.run import SyncRun, error_text, gateway_units, in_turn
from grove.pricing import COUNTERS, PriceBook
from grove.utils import utc_today

MONEY = ("cost", "user_spent", "user_balance")


class Usage(SyncRun):
	"""Every gateway Redis drained once — a store through its first writer that answers — into
	today's Usage Records."""

	sync_type = "Usage"

	def __init__(self, gateways=None, trigger="Scheduled", wait=0):
		super().__init__(trigger, wait)
		self.gateways = gateways

	def units(self):
		return gateway_units(self.gateways)

	def work(self, unit):
		return in_turn(unit, fetch_usage)

	def settle(self, unit, result):
		"""Landed the moment it arrives: the gateway has already deleted the counters."""
		return record_drain(*super().settle(unit, result), redis=unit.store)


def pull_all(gateways=None, trigger="Scheduled", wait=0):
	"""Scheduled: pull + drain every gateway Redis. Named `gateways` are pulled themselves. Skips if
	another pull is in flight, unless told to wait for it."""
	return Usage(gateways, trigger, wait).run()


def fetch_usage(target):
	"""GET /usage off one gateway, which reads-and-deletes each counter as it answers. The drained
	hashes ride along as `usages`. Pool thread: no frappe."""

	def drain(row):
		# Longer than a push: a timeout here is a deleted counter, not a retry next tick.
		row["usages"] = target.get("usage", timeout=15).get("usages", {})

	return target.dial(drain, had_data=0, usages={})


def record_drain(outcome, rows, redis=None):
	"""Land what a group drained — main thread, as soon as it arrives. The gateway has already
	deleted the counters, so a failure here keeps the payload as a Lost Usage row for the hourly
	replay and fails only this group's row. `redis` is the store drained, or None for a box on its own."""
	for row in rows:
		usages = row.pop("usages", None)
		if not row.get("success"):
			continue
		start = time.monotonic()
		try:
			pulled, lost = record_usages(row["server"], usages, redis=redis)
			row["had_data"] = 1 if pulled else 0
			row["detail"] = f"pulled:{pulled} lost:{lost}" if lost else f"pulled:{pulled}"
		except Exception as e:
			frappe.db.rollback()
			record_lost(row["server"], redis, utc_today(), usages)
			frappe.db.commit()
			row["success"], row["error"], outcome = 0, error_text(e), False
		row["duration_ms"] += int((time.monotonic() - start) * 1000)
	return outcome, rows


def record_usages(proxy_name, usages, redis=None, day=None):
	"""Record one gateway's drained deltas under `day` (today unless a replay says otherwise) and
	commit, then reconcile each touched user once. Everything lands under the Redis drained — the
	gateway's store, or the box itself — because the boxes on a store share one set of counters.
	→ (keys pulled, keys whose delta could not be recorded)."""
	if not usages:
		return 0, 0

	day = day or utc_today()
	# A named gateway is dialled itself with no store on its unit; its usage is still its store's.
	redis = redis or snapshot.gateway_redis(proxy_name)
	drained, lost = {}, 0
	for prefix, h in usages.items():
		drain = parse_drain(h)
		# Unregistered keys are dropped: the gateway already deleted the counter on read.
		if (not drain.requests and not drain.counters) or not (user := frappe.db.get_value("Grove API Key", prefix, "user")):
			continue
		# One key's failure must not take the rest of the response down with it — the gateway has
		# already deleted every counter in it. Roll back only that key's partial writes, keep going,
		# and keep the drained payload where the hourly replay finds it.
		frappe.db.savepoint("usage_key")
		try:
			add_delta(redis, prefix, user, day, drain.requests, drain.counters)
		except Exception:
			frappe.db.rollback(save_point="usage_key")
			record_lost(proxy_name, redis, day, {prefix: h}, api_key=prefix, grove_user=user)
			lost += 1
			continue
		drained.setdefault(user, []).append(drain)

	# Once per user, after the loop: the balance is the person's, and a user holding ten keys is
	# one sum, not ten. One user's failure skips nobody.
	reconciler = Reconciler(PriceBook.load(), day, redis, proxy_name)
	for user, drains in drained.items():
		frappe.db.savepoint("reconcile")
		try:
			reconciler.user(user, drains)
		except Exception:
			frappe.db.rollback(save_point="reconcile")
			frappe.log_error(title=f"Reconcile failed: {user} via {proxy_name}"[:140])

	frappe.db.commit()
	return len(usages), lost


def parse_drain(h):
	"""One drained hash split three ways: the key's request count, the per-(model, counter)
	quantities the rate tables price, and the money the gateway wrote (absent on an old gateway).
	`m:cost:<model>` is the box's own price per model, an audit input."""
	requests = int(h.get("request_count", 0) or 0)
	counters, model_cost = {}, {}
	for k, v in h.items():
		if not k.startswith("m:"):
			continue
		metric, _, model = k[2:].partition(":")  # model may contain ':' — keep the rest
		if not model:
			continue
		if metric == "cost":
			model_cost[model] = int(v or 0)
		elif metric in COUNTERS:
			counters.setdefault(model, {})[metric] = int(v or 0)
	money = {f: int(h[f]) for f in MONEY if f in h}
	return frappe._dict(requests=requests, counters=counters, model_cost=model_cost, money=money)


def add_delta(redis, prefix, user, day, requests, counters=None):
	"""ADD a pulled delta into the (api_key, day) Usage Record: requests onto its per-Redis row,
	amounts onto its per-(model, counter) rows. Rows that exist are incremented in place — an UPDATE
	that adds, no doc load, no save, so a pull costs a few statements per key instead of a full save
	with its child-table diff. Only a row that is not there yet goes through the Document API,
	which is what knows how to create one."""
	# Only a name Grove holds as a Model gets rows (published or not): the gateway also keys every
	# counter by deployment, and a Link row to anything else would fail the whole key's delta.
	models = list((counters or {}).keys())
	known = set(frappe.get_all("Model", filters={"name": ("in", models)}, pluck="name")) if models else set()
	counters = {m: c for m, c in (counters or {}).items() if m in known}

	name = ensure_rows(prefix, user, day, redis, counters)
	now = frappe.utils.now()
	frappe.db.sql(
		"update `tabUsage Gateway Row` set request_count = request_count + %s, last_pulled = %s "
		"where parent = %s and redis = %s",
		[requests, now, name, redis],
	)
	for model, counts in counters.items():
		for counter, amount in counts.items():
			frappe.db.sql(
				"update `tabUsage Counter Row` set amount = amount + %s "
				"where parent = %s and model = %s and counter = %s",
				[amount, name, model, counter],
			)
	# The UPDATE stamps modified itself, so the list view shows when usage last landed.
	frappe.db.sql("update `tabUsage Record` set user = %s, modified = %s where name = %s", [user, now, name])


def ensure_rows(prefix, user, day, redis, counters):
	"""The record and every row this delta lands in, created at zero where missing. The rare
	path — first sight of a key today, of a Redis or of a (model, counter) — and the only one
	that saves a document."""
	name = frappe.db.exists("Usage Record", {"day": day, "api_key": prefix})
	have_redis = name and frappe.db.exists("Usage Gateway Row", {"parent": name, "redis": redis})
	have = {
		(row.model, row.counter)
		for row in frappe.get_all(
			"Usage Counter Row", filters={"parent": name}, fields=["model", "counter"], parent_doctype="Usage Record"
		)
	} if (name and counters) else set()
	missing = [(model, counter) for model, counts in counters.items() for counter in counts if (model, counter) not in have]
	if name and have_redis and not missing:
		return name

	doc = frappe.get_doc("Usage Record", name) if name else frappe.new_doc("Usage Record")
	doc.api_key, doc.day, doc.user = prefix, day, user
	if not have_redis:
		doc.append("gateway_usage", {"redis": redis})
	for model, counter in missing:
		doc.append("counter_usage", {"model": model, "counter": counter})
	doc.save(ignore_permissions=True)
	return doc.name
