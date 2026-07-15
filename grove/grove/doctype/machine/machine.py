# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class Machine(Document):
	def validate(self):
		# gpu_count is derived — always the number of registered GPU rows.
		self.gpu_count = len(self.gpus or [])
