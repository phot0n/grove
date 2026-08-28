# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""One record per physical card or MIG slice, and the claim that says who holds it.

The claim is a COLUMN here rather than a row of its own, and that is the whole design. A separate
claim row has to name the card it holds, which means inventing a name: a UUID makes an unreadable
docname, a CUDA index cannot express a MIG slice at all, and a truncated UUID trades a silent,
data-dependent failure for brevity. A column needs no name — and it cannot outlive the card, cannot
name a card that does not exist, and disappears when a scan prunes the row.

`held_by` is taken by compare-and-swap, so two replicas cannot both win one card without any lock."""

import frappe
from frappe.model.document import Document


class GPUUnavailable(frappe.ValidationError):
	"""A card this replica needs is held by someone else.

	Its own class, not a bare ValidationError: placement catches it to move to the next box, and
	catching ValidationError there would swallow every other reason a replica failed to save."""


class GPU(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		compute_capability: DF.Float
		device_id: DF.Data
		gpu_index: DF.Int
		gpu_type: DF.Link | None
		held_by: DF.Link | None
		machine: DF.Link
		vram_gb: DF.Int
	# end: auto-generated types

	@property
	def is_free(self):
		return not self.held_by


def claim(gpu, replica):
	"""Take this card for `replica`. True if it was free and is now ours.

	One statement decides it. `UPDATE` takes a CURRENT read rather than the transaction's snapshot,
	so this is atomic under REPEATABLE READ — which a `SELECT` then `UPDATE` would not be, because
	the select would happily report a card free that someone committed a claim on a moment ago.

	The read-back confirms the winner rather than reaching into `frappe.db._cursor.rowcount`: it
	sees this transaction's own write, and it says who holds the card rather than how many rows an
	UPDATE touched."""
	frappe.db.sql(
		"""update `tabGPU` set held_by = %(replica)s, modified = now()
		where name = %(gpu)s and (held_by is null or held_by = '')""",
		{"replica": replica, "gpu": gpu},
	)
	return frappe.db.get_value("GPU", gpu, "held_by") == replica


def release(gpu, replica):
	"""Give a card back, but only one this replica actually holds.

	Guarded on the holder so a stale caller cannot free a card that has since been claimed by
	somebody else — releasing someone else's card is how two engines end up on one GPU."""
	frappe.db.sql(
		"""update `tabGPU` set held_by = null, modified = now()
		where name = %(gpu)s and held_by = %(replica)s""",
		{"gpu": gpu, "replica": replica},
	)


def release_if_stale(gpu):
	"""Free a card whose holder is no longer entitled to it, and say whether it went.

	Stored ownership can drift: a worker dying between a status flip and the release leaves a card
	held by a replica that stopped, and nothing else would ever free it — the card reads Allocated
	forever and no placement can take it.

	Repaired at the point of contention rather than by a sweep, because the only moment anyone cares
	whether a claim is real is when someone else wants the card. The claim that follows still
	arbitrates, so two callers both clearing the same stale holder is safe."""
	from grove.grove.doctype.model_replica.model_replica import CLAIM_HOLDING_STATUSES

	holder = frappe.db.get_value("GPU", gpu, "held_by")
	if not holder:
		return False
	if frappe.db.get_value("Model Replica", holder, "status") in CLAIM_HOLDING_STATUSES:
		return False  # genuinely held — real contention, not drift
	release(gpu, holder)
	return True


def cards_on(machines, fields=None):
	"""Every card on these machines, holder included. One query for the whole fleet.

	The allocation panel and the scheduler both read cards this way, so neither can disagree with
	the other about what is free."""
	if not machines:
		return []
	return frappe.get_all(
		"GPU",
		filters={"machine": ("in", list(machines))},
		fields=fields or ["name", "machine", "gpu_type", "device_id", "gpu_index",
		                  "vram_gb", "held_by"],
		order_by="machine, gpu_index",
	)
