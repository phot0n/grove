# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Give every card a replica already holds a `GPU Claim`.

Ownership moved from a derivation — walk the replicas in a claiming status, read their GPU child
rows — to a row whose NAME is `<box>:<index>`, so the primary key is what stops two replicas
holding one card. Nothing enforced that before, so this backfill is also the first time a
pre-existing double-book becomes visible: the second claim cannot be inserted, and the pair is
printed rather than swallowed.

Idempotent: a card already claimed by the replica that should hold it is skipped, so a re-run
repairs rather than duplicates."""

import frappe

from grove.grove.doctype.model_replica.model_replica import GPU_CLAIMING_STATUSES


def execute():
	if not frappe.db.table_exists("GPU Claim"):
		return
	replicas = frappe.get_all(
		"Model Replica",
		filters={"status": ("in", GPU_CLAIMING_STATUSES)},
		fields=["name", "inference_server"],
	)
	if not replicas:
		return
	pinned = frappe.get_all(
		"Model Replica GPU",
		filters={"parent": ("in", [r.name for r in replicas]), "parenttype": "Model Replica"},
		fields=["parent", "gpu_index"],
		parent_doctype="Model Replica",
	)
	boxes = {r.name: r.inference_server for r in replicas}

	claimed, conflicts = 0, []
	for row in pinned:
		server = boxes.get(row.parent)
		if not server:
			continue
		name = f"{server}:{int(row.gpu_index)}"
		holder = frappe.db.get_value("GPU Claim", name, "model_replica")
		if holder == row.parent:
			continue  # an earlier run of this patch already placed it
		if holder:
			# Two replicas were holding one card. Only the first can keep it; the pair is named
			# so an operator can move one, rather than the loser silently losing its cards.
			conflicts.append(f"  GPU {row.gpu_index} on {server}: {holder} and {row.parent}")
			continue
		frappe.get_doc(
			{
				"doctype": "GPU Claim",
				"inference_server": server,
				"gpu_index": int(row.gpu_index),
				"model_replica": row.parent,
			}
		).insert(ignore_permissions=True)
		claimed += 1

	print(f"GPU Claim: {claimed} card(s) claimed across {len(replicas)} replica(s)")
	if conflicts:
		print("GPU Claim: cards held twice before this patch — resolve by hand:")
		print("\n".join(conflicts))
