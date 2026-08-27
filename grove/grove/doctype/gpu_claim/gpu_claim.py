# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GPUClaim(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		gpu_index: DF.Int
		inference_server: DF.Link
		machine: DF.Link
		model_replica: DF.Link
	# end: auto-generated types

	def autoname(self):
		"""`<machine>:<cuda index>`, e.g. `mc-blackwell:0`.

		The NAME is the claim. Two replicas cannot hold one card because the primary key cannot
		hold one value twice — a second insert raises DuplicateEntryError, and the placement that
		lost simply takes the next box its policy already ranked. That is the whole concurrency
		design: no advisory lock, no serialising worker, and no read-then-write window.

		Named for the MACHINE, not the Inference Server: a GPU is a `Machine GPU` row, and
		`Inference Server.machine` is neither unique nor enforced, so two servers naming one
		machine would otherwise be able to claim the same physical card twice over.

		A colon rather than a dash: a machine name already contains dashes (`mc-blackwell`), so
		only a character it cannot hold keeps the split unambiguous."""
		self.name = claim_name(self.machine, self.gpu_index)


def claim_name(machine, gpu_index):
	return f"{machine}:{int(gpu_index)}"


def claims_on(machines):
	"""Every claim held on these machines, as `{(machine, gpu_index): model_replica}`.

	One query for the whole fleet — the allocation panel and the scheduler both read claims this
	way, so neither walks replicas to work out what a box is holding."""
	if not machines:
		return {}
	return {
		(claim.machine, int(claim.gpu_index)): claim.model_replica
		for claim in frappe.get_all(
			"GPU Claim",
			filters={"machine": ("in", list(machines))},
			fields=["machine", "gpu_index", "model_replica"],
		)
	}


def release_if_stale(name):
	"""Drop a claim whose holder is no longer entitled to it, and say whether it went.

	Stored ownership can drift where the derived kind could not: a worker dying between the
	status flip and the release leaves a card claimed by a replica that stopped, and nothing
	would ever free it — the card reads Allocated forever and no placement can take it.

	Repaired at the point of contention rather than by a sweep: the only moment anyone cares
	whether this claim is real is when someone else wants the card. The insert that follows is
	still what arbitrates, so two callers both deciding to clear the same stale claim is safe."""
	from grove.grove.doctype.model_replica.model_replica import CLAIM_HOLDING_STATUSES

	holder = frappe.db.get_value("GPU Claim", name, "model_replica")
	if not holder:
		return False
	status = frappe.db.get_value("Model Replica", holder, "status")
	if status in CLAIM_HOLDING_STATUSES:
		return False  # genuinely held — this is real contention, not drift
	frappe.delete_doc("GPU Claim", name, ignore_permissions=True, force=True)
	return True
