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
		model_replica: DF.Link
	# end: auto-generated types

	def autoname(self):
		"""`<inference server>:<cuda index>`, e.g. `inf-blackwell:0`.

		The NAME is the claim. Two replicas cannot hold one card because the primary key cannot
		hold one value twice — a second insert raises DuplicateEntryError, and the placement that
		lost simply takes the next box its policy already ranked. That is the whole concurrency
		design: no advisory lock, no serialising worker, and no read-then-write window for two
		callers to slip through.

		A colon rather than a dash: a server name already contains dashes (`inf-blackwell`), so
		only a character it cannot hold keeps the split unambiguous."""
		self.name = f"{self.inference_server}:{int(self.gpu_index)}"


def claims_on(inference_servers):
	"""Every claim held on these boxes, as `{(inference_server, gpu_index): model_replica}`.

	One query for the whole fleet — the panel and the scheduler both read claims this way, so
	neither walks replicas to work out what a box is holding."""
	if not inference_servers:
		return {}
	return {
		(claim.inference_server, int(claim.gpu_index)): claim.model_replica
		for claim in frappe.get_all(
			"GPU Claim",
			filters={"inference_server": ("in", list(inference_servers))},
			fields=["inference_server", "gpu_index", "model_replica"],
		)
	}
