# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class Machine(Document):
	"""An on-prem / baremetal / VM box that Inference Servers are provisioned on. Cloud GPU
	pods are a separate, standalone Pod doctype — not backed by a Machine."""

	pass
