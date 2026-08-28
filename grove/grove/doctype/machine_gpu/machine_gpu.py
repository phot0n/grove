# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MachineGPU(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		device_id: DF.Data | None
		gpu: DF.Link
		gpu_index: DF.Int
		gpu_type: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		vram_gb: DF.Int
	# end: auto-generated types

	pass
