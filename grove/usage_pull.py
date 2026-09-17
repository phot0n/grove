# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Pull token usage from each Gateway Server into monthly Usage Records.

The gateway accumulates per-key deltas in `usage:<prefix>`. A pull GETs /usage, which atomically
reads-and-deletes each live counter (HGETALL + DEL in one Lua call), stamps the month from OUR
clock, and ADDs the delta into the (key, month) record.

**1-shot, no retry**: the drain deletes the counter as it returns it, so a crash between the
response and the commit loses that cycle's delta. Never double-count, rare bounded loss on failure.
Requests metered mid-pull land either fully in the snapshot or on the fresh key, never split.

Each run is logged as a Pathway Sync doc and serialized by that doc's own lock, so two overlapping
pulls cannot drain the same counters twice."""

import json
import time

import requests

import frappe

from grove.pathway_sync import _finalize, _new_run, sync_targets, try_in_turn
from grove.grove.doctype.grove_user.grove_user import monthly_budget, set_rate_limited
from grove.grove.doctype.usage_record.usage_record import billable_tokens, current_month, enforce_budget

TIMEOUT = 15
_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "request_count")


def pull_all(gateways=None, trigger="Scheduled", wait=0):
	"""Scheduled: pull + drain every gateway Redis — a store once, through its first writer that
	answers. Named `gateways` are pulled themselves. Skips if another pull is in flight, unless
	told to wait for it."""
	doc = _new_run("Usage", trigger)
	if not doc.acquire_lock(wait=wait):
		return None
	try:
		groups = sync_targets() if gateways is None else [(None, [gateway]) for gateway in gateways]
		if not groups:
			return None
		ok = sum(try_in_turn(doc, store, gateways, _pull_and_classify) is True for store, gateways in groups)
		_finalize(doc, len(groups), ok)
		frappe.db.commit()
		return doc.name
	finally:
		doc.release_lock()


def reactivate_rate_limited():
	"""Daily: clear rate_limited for users back under their budget — the month rolled over, or the
	budget was raised. Still over for the current month stays blocked: the monthly cap is HARD.

	Runs independently of traffic, because a blocked user sees no new usage to re-fire the
	pull's budget check. Clearing unblocks every key they hold at once."""
	month = current_month()
	users = frappe.get_all("Grove User", filters={"rate_limited": 1}, pluck="name")
	cleared = 0
	for user in users:
		limit = monthly_budget(user)
		if limit and billable_tokens(user, month) >= limit:
			continue
		cleared += set_rate_limited(user, 0)
	if cleared:
		frappe.db.commit()
	return cleared


def _pull_and_classify(proxy_name):
	"""Pull + drain one proxy. Reachability and success are separate, like the keys/routes sync
	rows, so the log distinguishes 'down' from 'rejected'."""
	start = time.monotonic()
	reachable, success, http_status, error, detail, had_data = 1, 0, 0, None, "", 0
	try:
		pulled, lost = _pull_proxy(proxy_name)
		had_data = 1 if pulled else 0
		detail = f"pulled:{pulled} lost:{lost}" if lost else f"pulled:{pulled}"
		success = 1
	except (requests.ConnectionError, requests.Timeout) as e:
		reachable, error = 0, f"{type(e).__name__}: {e}"[:2000]
	except requests.HTTPError as e:
		http_status = e.response.status_code if e.response is not None else 0
		error = f"HTTP {http_status}: {e}"[:2000]
	except Exception as e:
		error = f"{type(e).__name__}: {e}"[:2000]
	return {
		"reachable": reachable,
		"success": success,
		"had_data": had_data,
		"http_status": http_status,
		"error": error,
		"duration_ms": int((time.monotonic() - start) * 1000),
		"detail": detail,
	}


def _pull_proxy(proxy_name):
	"""GET /usage, which atomically reads-and-deletes each counter, then record the deltas under
	this month and commit. → (keys pulled, keys whose delta could not be recorded)."""
	p = frappe.get_doc("Gateway Server", proxy_name)
	admin_url = (p.admin_url or "").rstrip("/")
	token = p.get_password("admin_token")

	r = requests.get(
		f"{admin_url}/usage", headers={"X-Grove-Admin-Token": token}, timeout=TIMEOUT
	)
	r.raise_for_status()
	usages = r.json().get("usages", {})
	if not usages:
		return 0, 0

	month = current_month()
	touched, lost = set(), 0
	for prefix, h in usages.items():
		amounts = {f: int(h.get(f, 0) or 0) for f in _FIELDS}
		per_model = _per_model(h)
		# Unregistered keys are dropped: the gateway already deleted the counter on read.
		if not any(amounts.values()) or not (user := frappe.db.get_value("Grove API Key", prefix, "user")):
			continue
		# One key's failure must not take the rest of the response down with it — the gateway has
		# already deleted every counter in it. Roll back only that key's partial writes, keep going,
		# and put the drained payload where someone can replay it.
		frappe.db.savepoint("usage_key")
		try:
			_add_delta(proxy_name, prefix, user, month, amounts, per_model)
		except Exception:
			frappe.db.rollback(save_point="usage_key")
			_log_lost_delta(proxy_name, prefix, user, month, h)
			lost += 1
			continue
		touched.add(user)

	# Once per user, after the loop: the budget is the person's, and a user holding ten keys
	# is one sum, not ten.
	for user in touched:
		enforce_budget(user, month)

	frappe.db.commit()
	return len(usages), lost


def _log_lost_delta(proxy_name, prefix, user, month, usage):
	"""The gateway deleted this counter on read, so the Error Log is now the only copy: the
	traceback, then the drained hash verbatim — enough to replay by hand."""
	payload = {"gateway_server": proxy_name, "api_key": prefix, "user": user, "month": month, "usage": usage}
	frappe.log_error(
		title=f"Usage delta lost: {prefix} on {proxy_name}"[:140],
		message=f"{frappe.get_traceback()}\n\nDrained payload (replay by hand):\n{json.dumps(payload, indent=1)}",
	)


def _per_model(h):
	"""Split the per-model breakdown out of a drained usage hash. The gateway writes
	per-model counters as `m:<metric>:<model>` fields alongside the flat aggregate, so
	one atomic drain carries both. → {model: {metric: delta}}."""
	per_model = {}
	for k, v in h.items():
		if not k.startswith("m:"):
			continue
		metric, _, model = k[2:].partition(":")  # model may contain ':' — keep the rest
		if metric in _FIELDS and model:
			per_model.setdefault(model, {})[metric] = int(v or 0)
	return per_model


def _add_delta(proxy_name, prefix, user, month, amounts, per_model=None):
	"""ADD a pulled delta into the (api_key, month) Usage Record: its per-gateway row, its
	per-model rows and the totals. Rows that exist are incremented in place — an UPDATE that
	adds, no doc load, no save, so a pull costs a few statements per key instead of a full save
	with its child-table diff. Only a row that is not there yet goes through the Document API,
	which is what knows how to create one. Totals stay the sum of the gateway rows because every
	gateway increment is mirrored onto them."""
	# Skip models no longer in the Model doctype so a stale name cannot fail the whole pull;
	# their tokens still land in the flat totals.
	models = list((per_model or {}).keys())
	known = set(frappe.get_all("Model", filters={"name": ("in", models)}, pluck="name")) if models else set()
	per_model = {m: d for m, d in (per_model or {}).items() if m in known}

	name = _ensure_rows(prefix, user, month, proxy_name, per_model)
	now = frappe.utils.now()
	_increment("Usage Gateway Row", amounts, now, parent=name, gateway_server=proxy_name)
	for model, deltas in per_model.items():
		_increment("Usage Model Row", {f: int(deltas.get(f, 0)) for f in _FIELDS}, now, parent=name, model=model)
	frappe.db.sql(
		f"update `tabUsage Record` set user = %s, modified = %s, {_adds()} where name = %s",
		[user, now, *[amounts[f] for f in _FIELDS], name],
	)


def _ensure_rows(prefix, user, month, proxy_name, per_model):
	"""The record and every row this delta lands in, created at zero where missing. The rare
	path — first sight of a key this month, of a gateway or of a model — and the only one that
	saves a document."""
	name = frappe.db.exists("Usage Record", {"month": month, "api_key": prefix})
	have_gateway = name and frappe.db.exists("Usage Gateway Row", {"parent": name, "gateway_server": proxy_name})
	have_models = set(
		frappe.get_all("Usage Model Row", filters={"parent": name}, pluck="model", parent_doctype="Usage Record")
	) if (name and per_model) else set()
	missing_models = [m for m in per_model if m not in have_models]
	if name and have_gateway and not missing_models:
		return name

	doc = frappe.get_doc("Usage Record", name) if name else frappe.new_doc("Usage Record")
	doc.api_key, doc.month, doc.user = prefix, month, user
	if not have_gateway:
		doc.append("gateway_usage", {"gateway_server": proxy_name})
	for model in missing_models:
		doc.append("model_usage", {"model": model})
	doc.save(ignore_permissions=True)
	return doc.name


def _adds():
	"""`prompt_tokens = prompt_tokens + %s, …` for every counter, in _FIELDS order."""
	return ", ".join(f"`{f}` = `{f}` + %s" for f in _FIELDS)


def _increment(doctype, amounts, now, **where):
	"""ADD `amounts` onto the one child row `where` names. Column names come from _FIELDS and
	the row filter's keys are this module's own literals, so only the values are parameters."""
	clause = " and ".join(f"`{k}` = %s" for k in where)
	frappe.db.sql(
		f"update `tab{doctype}` set {_adds()}, `last_pulled` = %s where {clause}",
		[*[amounts[f] for f in _FIELDS], now, *where.values()],
	)
