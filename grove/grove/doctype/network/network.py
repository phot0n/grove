# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe.model.document import Document

from grove.cloud_provider.base import CloudClientError, build_cloud_client

# Fixed ingress rules for the two security groups Create Security Groups can build.
PROXY_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"},
]
# 443 and nothing else for the workload: the box's engine proxy (nginx) fronts every vLLM
# instance under /e/<slug>/ and re-publishes the exporters under /metrics/*, so a box that serves
# ten models opens no more than one that serves one. The old 8080-8085 range was a guess that
# _assign_engine_port could outgrow — the seventh deployment on a box provisioned green and was
# unreachable — and it left 9100/9400 to be opened by hand.
INFERENCE_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"},
]

# Pool auto-assigned CIDR blocks are carved from — /16s never collide with each other, so any
# two Networks can be peered later without an overlap.
CIDR_POOL = ipaddress.ip_network("10.0.0.0/8")
CIDR_PREFIX = 16
# One public subnet per Network, carved off the front of its /16.
SUBNET_PREFIX = 24


class Network(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		availability_zone: DF.Data | None
		cidr_block: DF.Data | None
		cloud_provider: DF.Link | None
		inference_security_group_ids: DF.Data | None
		internet_gateway_id: DF.Data | None
		machine_image: DF.Data | None
		provider_type: DF.Data | None
		proxy_security_group_ids: DF.Data | None
		region: DF.Link
		route_table_id: DF.Data | None
		subnet_cidr_block: DF.Data | None
		subnet_id: DF.Data | None
		vpc_id: DF.Data | None
	# end: auto-generated types

	def validate(self):
		"""The whole address plan is derived: an operator picks a provider and a region, and
		Grove carves the ranges out. Nothing here is asked for, so every field below is
		read-only on the form."""
		if not self.cloud_provider:
			return
		if not self.cidr_block:
			self.cidr_block = self.next_available_cidr_block
		if not self.subnet_cidr_block:
			self.subnet_cidr_block = self.first_subnet_cidr_block

	@property
	def next_available_cidr_block(self):
		"""First /16 in 10.0.0.0/8 not already used by another Network."""
		filters = {"name": ["!=", self.name]} if not self.is_new() else {}
		used = {row for row in frappe.get_all("Network", pluck="cidr_block", filters=filters) if row}
		for block in CIDR_POOL.subnets(new_prefix=CIDR_PREFIX):
			if str(block) not in used:
				return str(block)
		frappe.throw(f"No /16 CIDR block available within {CIDR_POOL}.")

	@property
	def first_subnet_cidr_block(self):
		"""First /24 of this Network's VPC CIDR — the rest of the /16 is left for subnets Grove
		does not create today."""
		try:
			block = ipaddress.ip_network(self.cidr_block)
		except ValueError as e:
			frappe.throw(f"CIDR Block '{self.cidr_block}' on Network {self.name} is not valid: {e}")
		return str(next(block.subnets(new_prefix=SUBNET_PREFIX)))

	@property
	def proxy_security_group_id_list(self):
		"""proxy_security_group_ids as a list, for a Proxy Server box."""
		return parse_security_group_ids(self.proxy_security_group_ids)

	@property
	def inference_security_group_id_list(self):
		"""inference_security_group_ids as a list, for an Inference Server box."""
		return parse_security_group_ids(self.inference_security_group_ids)

	@property
	def cloud_client(self):
		"""CloudClient for this Network's account (its Cloud Provider's keys and kind) and
		region. Never a concrete class by name — build_cloud_client picks one."""
		if not self.cloud_provider:
			frappe.throw(f"Network {self.name} has no Cloud Provider set — it's a bare-metal placeholder.")
		provider = frappe.get_doc("Cloud Provider", self.cloud_provider)
		secret = provider.get_password("api_key", raise_exception=False)
		if not (provider.access_key_id and secret):
			frappe.throw(f"Cloud Provider {provider.name} has no credentials set.")
		if not self.region:
			frappe.throw(f"Network {self.name} has no Region set.")
		try:
			return build_cloud_client(provider.provider_type, provider.access_key_id, secret, self.region)
		except CloudClientError as e:
			frappe.throw(str(e))

	@frappe.whitelist()
	def create_network(self):
		"""Button: create this Network's VPC and public subnet on AWS — a route to an Internet
		Gateway and auto-assigned public IPs, so a launched Machine is reachable over SSH —
		then its security groups too, so one click gets a Network fully ready for a Machine."""
		if self.vpc_id:
			frappe.throw(f"Network {self.name} already has a VPC ID set.")

		network = self.cloud_client.create_network(
			self.name, self.cidr_block, self.subnet_cidr_block, self.availability_zone
		)
		self.db_set({
			"vpc_id": network["vpc_id"],
			"subnet_id": network["subnet_id"],
			"internet_gateway_id": network["internet_gateway_id"],
			"route_table_id": network["route_table_id"],
			"availability_zone": network["availability_zone"],
		})
		frappe.msgprint(f"VPC and subnet created for {self.name}.")
		self.create_security_groups()

	@frappe.whitelist()
	def create_security_groups(self):
		"""Button: create this Network's Proxy and Inference security groups on AWS, with their
		fixed ingress rules, and record the ids. Skips a role whose field is already set, so
		re-clicking after one is filled in by hand never creates a duplicate. Also runs as the
		second half of Create Network.

		Only ever creates. There is no revoke path here, so a Network whose groups already exist
		keeps whatever rules they hold — a fleet built before the engine proxy has to have 443
		added and the old engine range removed by hand."""
		if not self.vpc_id:
			frappe.throw(f"Set a VPC ID on Network {self.name} before creating security groups.")

		if not self.proxy_security_group_ids:
			proxy_sg_id = self.cloud_client.create_security_group(
				f"{self.name}-proxy", "Grove-managed: SSH + gateway (80/443)", self.vpc_id
			)
			self.cloud_client.authorize_ingress(proxy_sg_id, PROXY_INGRESS_RULES)
			self.db_set("proxy_security_group_ids", proxy_sg_id)

		if not self.inference_security_group_ids:
			inference_sg_id = self.cloud_client.create_security_group(
				f"{self.name}-inference", "Grove-managed: SSH + engine proxy (443)", self.vpc_id
			)
			self.cloud_client.authorize_ingress(inference_sg_id, INFERENCE_INGRESS_RULES)
			self.db_set("inference_security_group_ids", inference_sg_id)

		frappe.msgprint(f"Security groups created for {self.name}.")


def parse_security_group_ids(raw):
	"""A comma-separated security_group_ids field into a list, blanks and whitespace stripped."""
	return [group.strip() for group in (raw or "").split(",") if group.strip()]
