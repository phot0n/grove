# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document

from grove.cloud_provider.route53 import Route53Client, Route53Error
from grove.fleet import (
	GATEWAY_DNS_SETTINGS,
	gateway_health_checks_enabled,
	gateway_latency_routing_enabled,
)
from grove.tls import dns_credentials
from grove.utils import slugify

# `ap-south-1`, `us-gov-west-1`. A Region is named with its PROVIDER's code, and RunPod's are not
# AWS's, so this only decides whether the name can double as the latency reference.
AWS_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d")


class Region(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cloud_provider: DF.Data | None
		health_check_id: DF.Data | None
		label: DF.Data | None
		latency_reference: DF.Data | None
	# end: auto-generated types

	def validate(self):
		if not self.latency_reference and AWS_REGION.fullmatch(self.name or ""):
			self.latency_reference = self.name

	@property
	def dns_label(self):
		"""This region's label in DNS, and the SetIdentifier of its latency row. Slugged, because a
		Region carries its provider's own code and RunPod's are uppercase."""
		return slugify(self.name)

	@property
	def caller_reference(self):
		"""Idempotency token for this region's calculated health check."""
		return f"grove-region-{self.dns_label}"

	def sync_gateway_dns(self, exclude=None):
		"""Recompute the tier above the boxes: the calculated check that says whether this region is
		up at all, and the latency row that sends nearby clients to it.

		Called by a Gateway Server right after it writes or removes its own row, so the first gateway
		in a region creates the pair and the last one out takes it away. `exclude` drops one box
		(used from on_trash, where the row is still in the database during the delete)."""
		settings = frappe.get_single("Grove Settings")
		if not all(settings.get(field) for field in GATEWAY_DNS_SETTINGS):
			return None
		client = Route53Client(*dns_credentials(settings))
		# Simple mode has no region tier at all: every gateway's row sits in one multivalue set at the
		# shared name. The CNAME here would not merely be spare — Route53 refuses a CNAME beside a
		# record of any other type, so it has to go before those rows can exist.
		if not gateway_latency_routing_enabled():
			return self.remove_gateway_dns(client, settings)
		gateways = self.gateways(exclude)
		if not gateways:
			return self.remove_gateway_dns(client, settings)
		if not self.latency_reference:
			frappe.throw(
				f"Region {self.name} needs a Latency Reference: a latency record is measured against "
				f"an AWS region name, and '{self.name}' is not one. Name the nearest AWS region."
			)
		# The row goes first and the check it no longer names is dropped after, because Route53
		# refuses to delete a check while a record set still points at it. That is the path a fleet
		# takes when health checking is turned off: the row is rewritten without one, then the check
		# it used to name is released rather than left behind, billed and watching nothing.
		health_check_id = self.ensure_health_check(client, gateways)
		change = client.upsert_region_record(
			settings.fleet_zone,
			settings.gateway_host,
			self.dns_label,
			self.latency_reference,
			health_check_id,
		)
		if not health_check_id:
			self.delete_health_check(client)
		return change

	def gateways(self, exclude=None):
		"""Every gateway still in this region, with whatever check it carries. One query, because the
		region needs both answers out of it: whether any box is left at all, and which checks are the
		children of its own. `exclude` drops one box (used from on_trash, where the row is still in
		the database during the delete)."""
		filters = {"region": self.name, "status": ("!=", "Terminated")}
		if exclude:
			filters["name"] = ("!=", exclude)
		return frappe.get_all("Gateway Server", filters=filters, fields=["name", "health_check_id"])

	def ensure_health_check(self, client, gateways):
		"""This region's calculated check, created once and re-pointed as gateways come and go.

		Blank in two cases, and both mean the same thing to Route53 — the latency row resolves
		unconditionally. One is the fleet having health checking off. The other is every gateway here
		lacking a check of its own: a calculated check with no children and a threshold of 1 evaluates
		UNHEALTHY, which would take the whole region out of the latency set."""
		children = [gateway.health_check_id for gateway in gateways if gateway.health_check_id]
		if not children or not gateway_health_checks_enabled():
			return ""
		if self.health_check_id:
			client.update_calculated_health_check(self.health_check_id, children)
		else:
			self.db_set("health_check_id", client.create_calculated_health_check(self.caller_reference, children))
		return self.health_check_id

	def delete_health_check(self, client):
		"""Drop this region's calculated check and forget it. Only ever after the latency row has let
		go of it."""
		if self.health_check_id:
			client.delete_health_check(self.health_check_id)
			self.db_set("health_check_id", "")

	def remove_gateway_dns(self, client, settings):
		"""No gateway left here. The latency row comes off before the check it names, or Route53
		refuses to delete the check.

		Attempted whether or not a check was ever written: with health checking off the region still
		has a row, and reading a missing check as "nothing to remove" would leave that row pointing at
		an empty multivalue set — an answer with no address in it for everyone nearest this region. A
		row that was never there says InvalidChangeBatch, which is not a reason to keep a live one."""
		try:
			client.delete_region_record(
				settings.fleet_zone,
				settings.gateway_host,
				self.dns_label,
				self.latency_reference or self.name,
				self.health_check_id,
			)
		except Route53Error as e:
			if e.code != "InvalidChangeBatch":
				raise
		self.delete_health_check(client)
		return None


def sync_region_dns(region, exclude=None):
	"""Refresh one region's tier, by name. A box with no region has no tier to refresh."""
	if region and frappe.db.exists("Region", region):
		return frappe.get_doc("Region", region).sync_gateway_dns(exclude=exclude)
	return None


def dns_label(region):
	"""One region's DNS label without loading the doc — what a Gateway Server needs to name its own
	row in that region's set."""
	return slugify(region or "")
