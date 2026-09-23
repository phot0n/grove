# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure, tls
from grove.utils import is_label_under


class Geography(Document):
	"""A residency boundary. Traffic entering `endpoint` is served only by the boxes, pods and vendors
	in this geography, and a user pinned elsewhere is refused there. Its boxes are named under its own
	zone and serve its own wildcard certificate."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		endpoint: DF.Data
		fleet_tls_cert: DF.SmallText | None
		fleet_tls_expires_on: DF.Datetime | None
		fleet_tls_key: DF.Password | None
		fleet_tls_last_output: DF.Code | None
		fleet_tls_last_run: DF.Datetime | None
		fleet_zone: DF.Data
		label: DF.Data | None
	# end: auto-generated types

	def validate(self):
		self.validate_names()
		self.validate_fixed_names()

	def validate_names(self):
		for field in ("fleet_zone", "endpoint"):
			value = (self.get(field) or "").strip()
			self.set(field, value)

		if self.fleet_zone and not is_label_under(self.endpoint, self.fleet_zone):
			frappe.throw(
				f"Endpoint '{self.endpoint}' must be exactly one label under '{self.fleet_zone}' — "
				f"*.{self.fleet_zone} covers neither the zone itself nor anything deeper."
			)

	def validate_fixed_names(self):
		"""The gateways' DNS rows and names sit in the old values. Setting a zone for the first time is
		how a geography moves to TLS, so only changing one that was set is refused."""
		before = self.get_doc_before_save()
		moved = [
			field for field in ("endpoint", "fleet_zone")
			if before and before.get(field) and before.get(field) != self.get(field)
		]
		if moved and (gateways := self.gateways()):
			frappe.throw(f"{', '.join(gateways)} still answer at the old {' and '.join(moved)} — terminate them first.")

	def gateways(self):
		"""Every gateway still in this geography, by name."""
		return frappe.get_all(
			"Gateway Server", filters={"geography": self.name, "status": ("!=", "Terminated")}, pluck="name"
		)

	@property
	def tls_variables(self):
		"""Ansible vars a box here needs to front itself with TLS: the zone and its wildcard. Blank zone
		renders a box that serves :80 in the clear."""
		return {
			"fleet_zone": self.fleet_zone or "",
			"fleet_tls_cert": self.fleet_tls_cert or "",
			"fleet_tls_key": self.get_password("fleet_tls_key", raise_exception=False) or "",
		}

	@frappe.whitelist()
	def issue_fleet_certificate(self):
		"""Button: get this zone's wildcard from Let's Encrypt over DNS-01 and store it here. Does not
		ship it — provision and the daily renewal do that."""
		frappe.enqueue_doc(self.doctype, self.name, "_issue_fleet_certificate", queue="long", timeout=900)
		frappe.msgprint(f"Requesting a certificate for *.{self.fleet_zone} — this takes a minute.", alert=True)

	@failure.reports_failure()
	def _issue_fleet_certificate(self):
		tls.issue_fleet_certificate(self.name)
