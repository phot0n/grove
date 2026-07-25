# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class CloudProvider(Document):
	@frappe.whitelist()
	def fetch_gpu_types(self):
		"""Refresh the cached provider GPU-type list in the background. The heavy provider
		API call runs in a job that writes gpu_types (+ gpu_types_updated) on this doc; the
		form's HTML field renders it on reload. RunPod only."""
		if self.provider_type != "runpod":
			frappe.throw("GPU type listing is only available for RunPod providers.")
		if not self.get_password("api_key", raise_exception=False):
			frappe.throw("Set an API key on this Cloud Provider first.")
		frappe.enqueue(
			"grove.grove.doctype.cloud_provider.cloud_provider.update_gpu_types",
			queue="short",
			timeout=120,
			cloud_provider=self.name,
		)
		frappe.msgprint("Fetching GPU types in the background — reload the page in a few seconds.")


def update_gpu_types(cloud_provider):
	"""Job: pull the provider's GPU types (id + name + VRAM + cloud availability) and cache
	them on the Cloud Provider as JSON. Secure-cloud-available first (Grove uses Secure),
	then largest VRAM first — the same order the picker shows."""
	from grove.cloud_provider.runpod import RunPodClient

	cp = frappe.get_doc("Cloud Provider", cloud_provider)
	key = cp.get_password("api_key", raise_exception=False)
	if not key:
		return
	types = RunPodClient(key).list_gpu_types()
	types.sort(key=lambda g: (not g.get("secureCloud"), -(g.get("memoryInGb") or 0)))
	# db_set (no validate/version churn); gpu_types is a read-only cache field.
	cp.db_set("gpu_types", json.dumps(types), update_modified=False)
	cp.db_set("gpu_types_updated", frappe.utils.now(), update_modified=False)
	frappe.db.commit()
