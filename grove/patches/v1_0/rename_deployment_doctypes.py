# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Swap the two deployment names: the replica becomes `Model Replica`, and the service that was
`Deployment Template` takes `Model Deployment`.

Runs in `[pre_model_sync]` because it is a SWAP, not a rename — `Model Deployment` has to be
vacated before doctype sync recreates it from the renamed directory, or the two collide.

`rename_doc` moves the table, rewrites Link/Table `options` fleet-wide and updates child
`parenttype` values. What it cannot reach is a doctype name held somewhere that is not a doctype
table — `__Auth`, where every replica's `internal_api_key` is filed by doctype name. That one is
load-bearing: miss it and `get_password` throws inside `_gateway_routes`, so the fleet gets no
route push at all.

Every step is guarded or idempotent, so re-running this on an already-migrated bench repairs it
rather than damaging it:

    bench --site <site> execute grove.patches.v1_0.rename_deployment_doctypes.execute"""

import re

import frappe

from grove.naming import next_deployment_name


def execute():
	# `inference_server` is the replica's own column: its presence proves this doctype is still
	# the old replica rather than an already-swapped service. Also what makes the patch re-runnable.
	if frappe.db.has_column("Model Deployment", "inference_server"):
		frappe.rename_doc("DocType", "Model Deployment", "Model Replica", force=True)
		frappe.rename_doc("DocType", "Model Deployment GPU", "Model Replica GPU", force=True)

	# `__Auth` is keyed by doctype NAME and is not a doctype table, so `rename_doc` never reaches
	# it — every replica's `internal_api_key` would stay filed under the old name and
	# `get_password` would throw. That breaks `_gateway_routes` outright, so the fleet gets no
	# route push at all.
	#
	# Unguarded on purpose, which is what makes it a repair as well as a migration: Model
	# Deployment carries no Password field either before or after the swap, so a row still filed
	# under that name is always a replica's, whichever side of the rename this runs on.
	frappe.db.sql(
		"update `__Auth` set doctype = 'Model Replica' where doctype = 'Model Deployment'"
	)

	# A bench that already migrated the unshipped Deployment Template has one to promote. A bench
	# that never did gets its Model Deployment created by model sync instead.
	if frappe.db.exists("DocType", "Deployment Template"):
		frappe.rename_doc("DocType", "Deployment Template", "Model Deployment", force=True)

	# The replica's link field renamed with the doctype it points at. Without this, model sync adds
	# a fresh `model_deployment` column and every replica silently loses the service it belongs to.
	if frappe.db.has_column("Model Replica", "deployment_template"):
		frappe.db.rename_column("Model Replica", "deployment_template", "model_deployment")

	# Dynamic link stored as a string — `rename_doc` cannot see it.
	frappe.db.sql(
		"update `tabAnsible Play` set reference_doctype = 'Model Replica' "
		"where reference_doctype = 'Model Deployment'"
	)

	# Deployments an earlier run of this migration created were named `<model id>`, with `-1` for
	# a second shape. The format is now `MD-{#####}`. `gpus_per_replica` is the service's own
	# column, so its presence is what proves the promotion above has happened and this is not
	# still the old replica.
	if frappe.db.has_column("Model Deployment", "gpus_per_replica"):
		_raise_md_series_above_the_replicas()
		taken = set(frappe.get_all("Model Replica", pluck="name"))
		for old in frappe.get_all("Model Deployment", pluck="name"):
			if old.startswith("MD-") and old not in taken:
				continue
			# rename_doc carries every replica's `model_deployment` link across with it.
			frappe.rename_doc("Model Deployment", old, next_deployment_name(), force=True)

	for doctype in ("Model Replica", "Model Replica GPU"):
		frappe.reload_doc("grove", "doctype", frappe.scrub(doctype), force=True)


def _raise_md_series_above_the_replicas():
	"""Push the `MD-` counter past every `MD-{#####}` a replica already holds.

	Replicas predating the descriptive format are named `MD-00010`, but by an old Frappe
	`naming_series` whose `tabSeries` key is not `MD-` — so the counter a deployment draws from
	knows nothing about them and walks straight through numbers that are taken. Two doctypes are
	two tables, so nothing fails; the name just means both a deployment and a replica, and a route
	row's `deployment=` carries a replica name.

	One-time: no new replica is named `MD-{#####}`, so once the floor is above the legacy ones,
	every deployment name is unique for good."""
	numbers = [
		int(match.group(1))
		for name in frappe.get_all("Model Replica", pluck="name")
		if (match := re.fullmatch(r"MD-(\d+)", name))
	]
	if not numbers:
		return
	floor = max(numbers)
	if frappe.db.sql("select current from `tabSeries` where name = 'MD-'"):
		frappe.db.sql(
			"update `tabSeries` set current = %s where name = 'MD-' and current < %s",
			(floor, floor),
		)
	else:
		# getseries would otherwise create the row at 1 and hand out taken numbers.
		frappe.db.sql("insert into `tabSeries` (name, current) values ('MD-', %s)", (floor,))
