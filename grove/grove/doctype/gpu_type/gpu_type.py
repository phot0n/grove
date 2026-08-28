# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""One record per card TYPE, and the resolver that maps whatever a source called it onto that record.

Three sources name the same silicon three ways — nvidia-smi says `Tesla T4`, AWS says `T4`, RunPod
says `NVIDIA L40S` — and each writes its own VRAM, so one T4 is 15 GB and another 16. Every filter
comparing those strings or numbers is comparing whatever scanner wrote last. This is where they
agree instead.

A MIG slice resolves to its OWN type, not its parent's: `…A100-SXM4-80GB MIG 1g.10gb` is a different
thing to place on, with a different amount of memory, and calling it an A100 would offer a replica
80 GB that does not exist."""

import re

import frappe
from frappe.model.document import Document

# Dropped from the front of a reported name: they say who made the card, not which card it is. `Tesla` is nvidia's
# old product line rather than a vendor, but it prefixes exactly the silicon AWS reports bare —
# dropping it is what makes `Tesla T4` and `T4` one record.
VENDOR_WORDS = ("nvidia", "tesla")


class GPUType(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.gpu_type_alias.gpu_type_alias import GPUTypeAlias

		aliases: DF.Table[GPUTypeAlias]
		compute_capability: DF.Float
		vram_gb: DF.Int
	# end: auto-generated types

	def on_update(self):
		"""Push this type's figures onto its cards.

		`vram_gb` and `compute_capability` are `fetch_from` here, and a fetch only runs when the
		CARD is saved — a scan writes cards with `db.set_value`, so a figure corrected or seeded
		after a card existed would never reach it, and the placement checks read the card. This is
		what makes "correct it here and the whole fleet follows" true."""
		frappe.db.sql(
			"""update `tabGPU` set vram_gb = %(vram)s, compute_capability = %(capability)s
			where gpu_type = %(type)s""",
			{"vram": self.vram_gb or 0, "capability": self.compute_capability or 0,
			 "type": self.name},
		)

	def knows(self, reported):
		"""Whether this card has already been seen under that spelling."""
		wanted = (reported or "").strip().lower()
		return any((row.alias or "").strip().lower() == wanted for row in self.aliases)


def type_name_from(reported):
	"""`Tesla T4` → `T4`, `NVIDIA L40S` → `L40S`, `T4` → `T4`.

	Strip the vendor word, collapse the whitespace, keep the rest as the source wrote it. That
	collapses the spellings differing only by who was asked, which is most of them, and leaves a
	name a person would recognise — the record's name IS how the card is shown, so a slug would put
	`rtx-pro-6000-blackwell-server-edition` in every grid.

	It cannot collapse everything: `NVIDIA H100 80GB HBM3` keeps its suffix, because nothing here
	can know that is a memory configuration rather than a different card. That is what the alias
	table is for — derive the easy ones, record the rest by hand once."""
	words = re.split(r"\s+", (reported or "").strip())
	while words and words[0].lower() in VENDOR_WORDS:
		words = words[1:]
	return " ".join(words)


def resolve(reported, vram_gb=None, compute_capability=None):
	"""The `GPU Type` a source's model string means, creating it the first time the fleet meets a card.

	Learns as it goes: a spelling that resolved through the derived name is recorded as an alias, so
	the next lookup is a hit rather than a re-derivation, and an operator can see every name the
	fleet has reported for one card.

	Never raises on an unknown card. A scan meeting new silicon has to record it and carry on —
	failing would leave the box with no inventory at all, which is worse than a record nobody has
	corrected yet."""
	reported = (reported or "").strip()
	if not reported:
		return None
	name = match_alias(reported) or type_name_from(reported)
	if not name:
		return None

	if frappe.db.exists("GPU Type", name):
		doc = frappe.get_doc("GPU Type", name)
		changed = False
		# Seeded only where there is nothing. A scanner must never overwrite what an operator
		# corrected — the sources disagree with each other, so last-write-wins would make the
		# figure depend on which box was rescanned most recently.
		if vram_gb and not doc.vram_gb:
			doc.vram_gb = vram_gb
			changed = True
		if compute_capability and not doc.compute_capability:
			doc.compute_capability = compute_capability
			changed = True
		if not doc.knows(reported):
			doc.append("aliases", {"alias": reported})
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return doc.name

	return (
		frappe.get_doc(
			{
				"doctype": "GPU Type",
				"name": name,
				"vram_gb": vram_gb or 0,
				"compute_capability": compute_capability or 0,
				"aliases": [{"alias": reported}],
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def match_alias(reported):
	"""The type already carrying this exact spelling, if any.

	Checked before deriving, so an alias someone added by hand beats what would be derived — the
	only route by which `NVIDIA H100 80GB HBM3` ever reaches `H100`."""
	rows = frappe.get_all(
		"GPU Type Alias",
		filters={"alias": reported, "parenttype": "GPU Type"},
		fields=["parent"],
		parent_doctype="GPU Type",
		limit=1,
	)
	return rows[0].parent if rows else None
