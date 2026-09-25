# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Scheduled reconcile of the cloud fleet. The PROVIDER owns whether a pod or instance is up, but
the only things that read it back are lifecycle jobs, and those end — a bring-up that outran its
poll, a stop from the console, a worker killed mid-spawn all leave a doc the provider disagrees
with. This re-reads both fleets on a timer so drift closes on its own.

Each doc is synced in isolation: one unreachable pod must not stop the rest."""

import frappe

from grove.cloud_provider.provisioner import PodProvisioner


def sync_all():
	"""Scheduled entry point: re-read every live Pod and Machine off its provider."""
	sync_pods()
	sync_machines()


def sync_pods():
	"""Pods with a provider pod behind them. The routes a status change moves are projected by the
	scheduled tick."""
	for name in frappe.get_all(
		"Pod",
		filters={"pod_id": ("!=", ""), "status": ("!=", "Terminated")},
		pluck="name",
	):
		try:
			PodProvisioner(frappe.get_doc("Pod", name)).sync()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Scheduled pod sync failed: {name}")


def sync_machines():
	"""Machine.sync cascades what it finds onto the servers built on the box."""
	for name in frappe.get_all(
		"Machine",
		filters={"instance_id": ("!=", ""), "status": ("!=", "Terminated")},
		pluck="name",
	):
		try:
			frappe.get_doc("Machine", name).sync()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Scheduled machine sync failed: {name}")
