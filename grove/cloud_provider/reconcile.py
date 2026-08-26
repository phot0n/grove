# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Scheduled reconcile of the cloud fleet. The provider — not Grove — owns whether a pod or an
instance is actually up, but the only things that read it back are lifecycle jobs, and those
end: a bring-up that outran its poll, a stop from the provider's console, a worker killed
mid-spawn all leave a doc saying something the provider disagrees with. This re-reads both
fleets on a timer so that drift closes on its own instead of waiting for someone to press Sync.

Each doc is synced in isolation — one unreachable pod must not stop the rest of the fleet from
reconciling."""

import frappe

from grove.cloud_provider.provisioner import PodProvisioner


def sync_all():
	"""Scheduled entry point: re-read every live Pod and Machine off its provider."""
	sync_pods()
	sync_machines()


def sync_pods():
	"""Pods with a provider pod behind them. Each in isolation — one unreachable pod must not stop
	the rest of the fleet. The routes a status change moves are projected by the scheduled tick."""
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
	"""Machines with a live instance. Machine.sync cascades what it finds onto the Proxy /
	Inference / Monitoring servers built on the box."""
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
