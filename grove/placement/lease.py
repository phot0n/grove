# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""A short-lived note in Redis saying "someone is taking this card right now".

Advisory only. `GPU.held_by` is what OWNS a card — durable, transactional with the replica that
holds it, and arbitrated by the compare-and-swap that sets it. A lease owns nothing and is safe to lose: if
Redis is flushed or evicted (`allkeys-lru`, no persistence), placement falls back to the behaviour
it had before leases existed. Correct, just slower. That asymmetry is the whole reason ownership
stays in the database and only the HINT lives here.

What it buys is the one thing no isolation level can give: visibility BEFORE commit.

A claim is invisible to every other transaction until it commits, and InnoDB locks the unique index
entry meanwhile — so a rival trying the same card does not fail fast, it BLOCKS for as long as the
winner's transaction stays open, holding a database connection while it waits. Under REPEATABLE
READ the rival cannot even see the claim after that commit, because its snapshot predates it.

A lease is written before the row and read outside any transaction, so a rival sees it immediately,
skips the card, and never queues behind anyone."""

import frappe

# Long enough to outlive a placement (scan, insert, commit) and the stale read window of a
# transaction that started just before it; short enough that a worker dying mid-placement frees
# the card again without anyone intervening. The TTL is the only cleanup — nothing tracks leases.
LEASE_TTL = 60


def _key(gpu):
	# Keyed on the GPU's docname, which is unique across the fleet — so no machine prefix, and
	# nothing here can be confused by a CUDA index moving between the ranking and the placement.
	#
	# Not run through frappe.cache.make_key, which is what prefixes db_name and separates one
	# site's cache from another's — the raw client is used below for SET NX, and raw calls skip
	# that. Fine while a bench serves one Grove site; two would share these keys, and a card name
	# colliding across them would have one site's placement skip the other's cards.
	return f"grove:gpu_lease:{gpu}"


def leased(gpus):
	"""Which of these cards someone else is currently taking.

	`get`, never `exists`. RedisWrapper leaves `set`/`get`/`delete` as the raw client but
	OVERRIDES `exists` to run the name through `make_key` first — so a lease written by `set`
	would be looked up under a different, db_name-prefixed key, always come back missing, and
	this would silently report every card free."""
	return {gpu for gpu in gpus if frappe.cache.get(_key(gpu)) is not None}


def take(gpus, holder):
	"""Announce that `holder` is taking these cards. True if every one of them was free.

	All or nothing: a partial lease would leave a card marked busy that nobody goes on to claim,
	so anything taken is handed back before returning False."""
	taken = []
	for gpu in gpus:
		if frappe.cache.set(_key(gpu), holder, nx=True, ex=LEASE_TTL):
			taken.append(gpu)
			continue
		release(taken)
		return False
	return True


def release(gpus):
	"""Hand cards back before the TTL — used when a placement failed, never when it succeeded.

	A successful placement leaves its lease to expire on its own: `held_by` is committed by
	then, but a rival whose snapshot predates that commit still cannot see it, and the lease is
	what keeps that rival off the card until its snapshot catches up."""
	for gpu in gpus:
		frappe.cache.delete(_key(gpu))
