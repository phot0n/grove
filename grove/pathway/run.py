# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""What every Pathway Sync run shares: one box's admin API, the units a run dials, the pool that
dials them, and the rows that land on the run doc. Every path that reaches a box goes through here.

Runs of one type serialize on the Pathway Sync doc's advisory lock, so a slow run cannot land a
stale write after a newer one. Boxes are dialled in parallel, a store's writers in turn, and rows
land in the order asked."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests

import frappe

TIMEOUT = 10
MAX_PARALLEL = 8
ERROR_LIMIT = 2000  # Pathway Sync Row.error is Small Text
NO_WRITER = "No Active Gateway Store Writer — mark one of this store's gateways, or nothing updates it."

# Redacted before a payload is written to a Pathway Sync Row, which anyone who can open the Desk
# can read.
SECRET_KEYS = frozenset({"internal_key", "key_hash", "admin_token", "data_token", "api_secret"})
# A log row, not an archive: a fleet-sized route table is megabytes.
PAYLOAD_LIMIT = 8000


def error_text(exc):
	return f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]


class InSync(Exception):
	"""Raised by a dial's `work` when the box already holds the state: nothing sent, no row."""


@dataclass(frozen=True)
class Target:
	"""One box's admin API, resolved on the main thread so dialling it never touches frappe. Both
	planes derive admin_url the same way and gate /admin on the same token."""

	server_type: str
	name: str
	admin_url: str = ""
	token: str = ""
	error: str | None = None

	@classmethod
	def of(cls, doc):
		if not doc.admin_url:
			frappe.throw(f"{doc.doctype} {doc.name} has no admin_url")
		return cls(doc.doctype, doc.name, doc.admin_url.rstrip("/"), doc.get_password("admin_token") or "")

	@classmethod
	def resolve(cls, server_type, name):
		"""A box that cannot be resolved keeps the reason, which becomes its row."""
		try:
			return cls.of(frappe.get_doc(server_type, name))
		except Exception as e:
			return cls(server_type, name, error=error_text(e))

	@property
	def headers(self):
		return {"X-Grove-Admin-Token": self.token}

	def get(self, path, timeout=TIMEOUT):
		response = requests.get(f"{self.admin_url}/{path}", headers=self.headers, timeout=timeout)
		response.raise_for_status()
		return response.json()

	def post(self, path, body):
		response = requests.post(f"{self.admin_url}/{path}", json=body, headers=self.headers, timeout=TIMEOUT)
		response.raise_for_status()
		return response.json()

	def remote_hashes(self):
		"""The hash map the box stored on its last accepted push. Empty on a wiped Redis, which is
		what makes every section read as drift and heal."""
		return self.get("state-hash").get("hashes") or {}

	def dial(self, work, **fields):
		"""`work(row)` against this box, the outcome classified onto the row: reachability and
		success are separate, so the log tells 'down' from 'rejected'. `fields` are on the row even
		when the box could not be resolved and `work` never ran. None on InSync — nothing to log.
		Pool thread: no frappe."""
		start = time.monotonic()
		row = {"reachable": 1, "success": 0, "http_status": 0, "error": self.error, "detail": "", **fields}
		try:
			if not self.error:
				work(row)
				row["success"] = 1
		except InSync:
			return None
		except (requests.ConnectionError, requests.Timeout) as e:
			row["reachable"], row["error"] = 0, error_text(e)
		except requests.HTTPError as e:
			row["http_status"] = e.response.status_code if e.response is not None else 0
			row["error"] = f"HTTP {row['http_status']}: {e}"[:ERROR_LIMIT]
		except Exception as e:
			row["error"] = error_text(e)
		row["duration_ms"] = int((time.monotonic() - start) * 1000)
		return row


@dataclass(frozen=True)
class Unit:
	"""What a run dials independently of the rest: a store's writers in turn, or one box alone."""

	store: str | None
	targets: tuple


def sync_targets():
	"""Where a run reaches each gateway Redis, as (store, gateways) groups: a gateway on its own
	Redis alone, then each store through its Active writers in the order they are tried. A store
	whose Active gateways include no writer is a group of none."""
	alone, stores = [], {}
	gateways = frappe.get_all(
		"Gateway Server",
		filters={"status": "Active"},
		fields=["name", "gateway_store", "is_store_writer"],
		order_by="name asc",
	)
	for gateway in gateways:
		if not gateway.gateway_store:
			alone.append((None, [gateway.name]))
			continue
		writers = stores.setdefault(gateway.gateway_store, [])
		if gateway.is_store_writer:
			writers.append(gateway.name)
	return alone + sorted(stores.items())


def gateway_units(gateways=None):
	"""One Unit per gateway Redis. A named gateway is dialled itself, whatever store it is on: that
	is an operator's button. None means every Active box — `is None` and not truthiness, because
	an empty list is a caller saying "no gateway work"."""
	groups = sync_targets() if gateways is None else [(None, [gateway]) for gateway in gateways]
	return [
		Unit(store, tuple(Target.resolve("Gateway Server", gateway) for gateway in group))
		for store, group in groups
	]


def in_turn(unit, work):
	"""`work(target)` on each of the unit's targets until one succeeds. → (outcome, rows): True when
	one succeeded, None when one already held the state (it leaves no row), False when none got
	through. Touches no doc, so a pool thread can run it: the caller appends the rows."""
	if not unit.targets:
		return False, [{"server_type": "Gateway Store", "server": unit.store, "error": NO_WRITER}]
	rows = []
	for target in unit.targets:
		res = work(target)
		if res is None:
			return None, rows
		rows.append({"server_type": target.server_type, "server": target.name, **res})
		if res["success"]:
			return True, rows
	return False, rows


def each_in_parallel(units, work):
	"""`work(unit)` for every unit, yielding (index, result) as each finishes. `work` runs on a pool
	thread, which has no frappe.local — no db, no docs, no frappe.throw — so it takes resolved
	inputs and returns plain data. One that raises anyway yields the exception as its result."""
	if len(units) <= 1:
		yield from ((index, guarded(work, unit)) for index, unit in enumerate(units))
		return
	with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(units))) as pool:
		futures = {pool.submit(guarded, work, unit): index for index, unit in enumerate(units)}
		for future in as_completed(futures):
			yield futures[future], future.result()


def guarded(work, unit):
	try:
		return work(unit)
	except Exception as e:
		return e


def settled(unit, result):
	"""A unit's (outcome, rows). Work that raised is a bug, not a box: it fails its own unit,
	named on the first box, and the run carries on."""
	if not isinstance(result, Exception):
		return result
	first = unit.targets[0] if unit.targets else Target("Gateway Store", unit.store)
	return False, [{"server_type": first.server_type, "server": first.name, "error": error_text(result)}]


def redact(value):
	"""Secrets replaced by a marker, structure intact. Marked rather than dropped: `"***"` says the
	key WAS sent, where a missing field would read as a push that forgot it."""
	if isinstance(value, dict):
		return {k: ("***" if k in SECRET_KEYS and v else redact(v)) for k, v in value.items()}
	if isinstance(value, list):
		return [redact(item) for item in value]
	return value


def payload_text(log):
	"""What a target was sent, as text for the row. Truncated rather than trimmed field by field: a
	table too big to store is itself worth seeing."""
	text = frappe.as_json(log or [])
	if len(text) > PAYLOAD_LIMIT:
		return f"{text[:PAYLOAD_LIMIT]}\n… truncated, {len(text)} characters in full"
	return text


def new_run(sync_type, trigger):
	doc = frappe.new_doc("Pathway Sync")
	doc.run_at = frappe.utils.now_datetime()
	doc.sync_type = sync_type
	doc.trigger = trigger
	return doc


def finalize(doc, total, ok):
	"""Both planes counted together — a run's targets are the boxes it actually pushed."""
	doc.targets_total = total
	doc.targets_ok = ok
	doc.status = "Success" if ok == total else ("Failed" if ok == 0 else "Partial")
	doc.insert(ignore_permissions=True)


class SyncRun:
	"""One Pathway Sync run. A subclass says which units it dials, what each does on a pool thread,
	and how an answer lands; the base takes the lock, dials in parallel, puts rows on the doc in the
	order asked, and finalizes only when a row was left."""

	sync_type = ""

	def __init__(self, trigger="Scheduled", wait=0):
		self.trigger = trigger
		self.wait = wait
		self.doc = None

	def run(self):
		"""→ the Pathway Sync name, or None when nothing was logged: a run already in flight, no
		boxes to dial, or a fleet already in sync."""
		self.doc = new_run(self.sync_type, self.trigger)
		if not self.doc.acquire_lock(wait=self.wait):  # scheduled → skip if a run is in flight; forced → queue
			return None
		try:
			units = self.units()
			if not units:
				return None
			answers = {}
			for index, result in each_in_parallel(units, self.work):
				answers[index] = self.settle(units[index], result)
			return self.record(units, answers)
		finally:
			self.doc.release_lock()

	def units(self):
		"""Main thread: everything a pool thread will need, resolved here."""
		raise NotImplementedError

	def work(self, unit):
		"""Pool thread — no frappe.local, no db, no docs. → (outcome, rows)."""
		raise NotImplementedError

	def settle(self, unit, result):
		"""A unit's (outcome, rows), on the main thread the moment it arrives."""
		return settled(unit, result)

	def record(self, units, answers):
		"""Rows onto the run doc in the order the units were asked, whichever finished first. A unit
		that left no row already held the state and is not a target of this run."""
		total = ok = 0
		for index, unit in enumerate(units):
			outcome, rows = answers[index]
			for row in rows:
				if "payload" in row:
					row["payload"] = payload_text(row["payload"])
				self.doc.append("results", row)
			if rows:
				total, ok = total + 1, ok + (outcome is not False)
		if self.doc.results:
			finalize(self.doc, total, ok)
		frappe.db.commit()
		return self.doc.name if self.doc.results else None
