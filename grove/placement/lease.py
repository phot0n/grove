# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""A short-lived note in Redis saying "someone is taking this card right now".

Advisory only. `GPU.held_by` is what OWNS a card. A lease owns nothing and is safe to lose: on a
flushed Redis (`allkeys-lru`, no persistence) placement falls back to the behaviour it had before
leases existed — correct, just slower. That asymmetry is why ownership stays in the database.

What it buys is the one thing no isolation level can give: visibility BEFORE commit. A claim is
invisible to other transactions until it commits and InnoDB locks its index entry meanwhile, so a
rival BLOCKS for as long as the winner's transaction stays open — and under REPEATABLE READ still
cannot see the claim afterwards, because its snapshot predates it.

A lease is written before the row and read outside any transaction, so a rival sees it immediately
and skips the card."""

import frappe

# Long enough to outlive a placement and the stale-read window of a transaction that started just
# before it, short enough that a dead worker frees the card. The TTL is the only cleanup.
LEASE_TTL = 60


def _key(gpu):
	# The docname is unique across the fleet, so no machine prefix — and nothing here can be
	# confused by a CUDA index moving between the ranking and the placement.
	#
	# Not run through make_key, which is what separates one site's cache from another's: the raw
	# client is used below for SET NX and raw calls skip it. Two Grove sites on one bench would
	# share these keys.
	return f"grove:gpu_lease:{gpu}"


def leased(gpus):
	"""Which of these cards someone else is currently taking.

	`get`, never `exists`: RedisWrapper leaves `set`/`get`/`delete` raw but OVERRIDES `exists` to
	run the name through `make_key`, so the lookup would use a different key, always miss, and
	silently report every card free."""
	return {gpu for gpu in gpus if frappe.cache.get(_key(gpu)) is not None}


def take(gpus, holder):
	"""Announce that `holder` is taking these cards. True if every one was free. All or nothing: a
	partial lease would leave a card marked busy that nobody goes on to claim."""
	taken = []
	for gpu in gpus:
		if frappe.cache.set(_key(gpu), holder, nx=True, ex=LEASE_TTL):
			taken.append(gpu)
			continue
		release(taken)
		return False
	return True


def release(gpus):
	"""Hand cards back before the TTL — only when a placement FAILED. A successful one leaves its
	lease to expire: a rival whose snapshot predates the claim's commit still cannot see it, and
	the lease is what keeps it off the card until then."""
	for gpu in gpus:
		frappe.cache.delete(_key(gpu))
