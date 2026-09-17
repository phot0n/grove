"""What every doc standing on a box shares."""

import frappe

from grove.ansible import AnsibleHost
from grove.naming import GeneratedName, short_name
from grove.utils import validate_id_safe_name


class Server(GeneratedName, AnsibleHost):
	"""A name off its Machine, plays against the box, and retirement with it. A doctype says
	what still depends on it in `archive_blockers`; the rest is the same for all of them."""

	def get_generated_name(self):
		"""The Machine's name: the box was named once, when it was created."""
		return self.machine

	def before_insert(self):
		# Generated names are id-safe by construction, but a Region named with a dot would slug
		# into one that is not.
		validate_id_safe_name(self.doctype, self.short_name)
		if self.short_name != self.name and self.name != f"{self.short_name}.{self.fleet_zone}":
			frappe.throw(
				f"{self.doctype} name '{self.name}' must be one label, or one label under its "
				f"Geography's zone ('{self.fleet_zone or 'none set'}')."
			)

	@property
	def short_name(self):
		return short_name(self.name)

	@property
	def fleet_zone(self):
		"""The zone this box is named under: its Geography's. Blank without one."""
		return frappe.db.get_value("Geography", self.geography, "fleet_zone") if self.geography else ""

	# def before_rename(self, old_name, new_name, merge=False):
	# 	validate_id_safe_name(self.doctype, new_name)

	@property
	def archive_blockers(self):
		return []

	@frappe.whitelist()
	def archive(self):
		"""Button: retire this server and destroy its box. Refused while anything still depends
		on it. A cloud Machine is terminated, which already cascades Terminated onto this row and
		drops its DNS records; an on-prem box has nothing to destroy, so only the row is retired."""
		if blockers := self.archive_blockers:
			frappe.throw("<br>".join(blockers), title=f"{self.name} cannot be archived")
		machine = frappe.get_doc("Machine", self.machine) if self.machine else None
		if machine and machine.cloud_provider:
			machine.terminate()

		frappe.msgprint(f"Archiving {self.name}.", alert=True)
