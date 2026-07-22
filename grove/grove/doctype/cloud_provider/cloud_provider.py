# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CloudProvider(Document):
	@frappe.whitelist()
	def fetch_gpu_types(self):
		"""List the provider's GPU types (id + name + VRAM + cloud availability) so a user
		can pick the exact gpu_model for a Machine's GPU rows. RunPod only."""
		if self.provider_type != "runpod":
			frappe.throw("GPU type listing is only available for RunPod providers.")
		from grove.cloud_provider.runpod import RunPodClient

		key = self.get_password("api_key", raise_exception=False)
		if not key:
			frappe.throw("Set an API key on this Cloud Provider first.")
		types = RunPodClient(key).list_gpu_types()
		# Secure-cloud-available first (Grove uses Secure), then largest VRAM first.
		types.sort(key=lambda g: (not g.get("secureCloud"), -(g.get("memoryInGb") or 0)))
		return types
