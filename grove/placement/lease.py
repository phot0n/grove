# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""A short-lived note in Redis saying "someone is taking this card right now".

Advisory only. The `GPU Claim` row is what OWNS a card — durable, transactional with the replica
that holds it, and arbitrated by its primary key. A lease owns nothing and is safe to lose: if
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


def _key(machine, gpu_index):
	# Not run through frappe.cache.make_key, which is what prefixes db_name and separates one
	# site's cache from another's — the raw client is used below for SET NX, and raw calls skip
	# that. Fine while a bench serves one Grove site; two would share these keys, and a machine
	# name colliding across them would have one site's placement skip the other's cards.
	return f"grove:gpu_lease:{machine}:{int(gpu_index)}"


def leased(machine, gpu_indexes):
	"""Which of these cards someone else is currently taking.

	`get`, never `exists`. RedisWrapper leaves `set`/`get`/`delete` as the raw client but
	OVERRIDES `exists` to run the name through `make_key` first — so a lease written by `set`
	would be looked up under a different, db_name-prefixed key, always come back missing, and
	this would silently report every card free."""
	return {index for index in gpu_indexes if frappe.cache.get(_key(machine, index)) is not None}


def take(machine, gpu_indexes, holder):
	"""Announce that `holder` is taking these cards. True if every one of them was free.

	All or nothing: a partial lease would leave a card marked busy that nobody goes on to claim,
	so anything taken is handed back before returning False."""
	taken = []
	for index in gpu_indexes:
		if frappe.cache.set(_key(machine, index), holder, nx=True, ex=LEASE_TTL):
			taken.append(index)
			continue
		release(machine, taken)
		return False
	return True


def release(machine, gpu_indexes):
	"""Hand cards back before the TTL — used when a placement failed, never when it succeeded.

	A successful placement leaves its lease to expire on its own: the `GPU Claim` is committed by
	then, but a rival whose snapshot predates that commit still cannot see it, and the lease is
	what keeps that rival off the card until its snapshot catches up."""
	for index in gpu_indexes:
		frappe.cache.delete(_key(machine, index))
