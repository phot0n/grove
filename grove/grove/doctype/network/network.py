# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe.model.document import Document

from grove.cloud_provider.base import build_cloud_client
from grove.grove.doctype.gateway_state_store.gateway_state_store import REDIS_PORT
from grove.monitoring import BOX_HTTP_PORT, BOX_HTTPS_PORT
from grove.net import reachable_ip

PROXY_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"},
]
# 443 is NOT here: it carries the engine proxy, which only the gateway and the metrics agent
# dial, so its sources are computed per box — see sync_inference_ingress.
INFERENCE_BASE_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
]

# /16s never collide, so any two Networks can be peered later without an overlap.
CIDR_POOL = ipaddress.ip_network("10.0.0.0/8")
CIDR_PREFIX = 16
# One public subnet per Network, carved off the front of its /16.
SUBNET_PREFIX = 24

# Both reconciled to the same sources. 80 is where the front is going — plain HTTP, reachable
# only from inside the fleet, so the box needs no certificate — and 443 is where it still is.
# 443 comes out once every box has moved.
FRONT_PORTS = (BOX_HTTP_PORT, BOX_HTTPS_PORT)

# A box in this state no longer exists, so its address is not one to keep a hole open for.
GONE_STATUS = "Terminated"


class Network(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		availability_zone: DF.Data | None
		cidr_block: DF.Data | None
		cloud_provider: DF.Link | None
		geography: DF.Link | None
		inference_security_group_ids: DF.Data | None
		internet_gateway_id: DF.Data | None
		machine_image: DF.Data | None
		provider_type: DF.Data | None
		proxy_security_group_ids: DF.Data | None
		region: DF.Link
		route_table_id: DF.Data | None
		state_store_security_group_ids: DF.Data | None
		subnet_cidr_block: DF.Data | None
		subnet_id: DF.Data | None
		vpc_id: DF.Data | None
	# end: auto-generated types

	def validate(self):
		"""The whole address plan is derived: an operator picks a provider and a region, Grove
		carves the ranges out, and every field below is read-only on the form."""
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
		"""First /24 of the VPC CIDR; the rest of the /16 is left for subnets Grove does not
		create today."""
		try:
			block = ipaddress.ip_network(self.cidr_block)
		except ValueError as e:
			frappe.throw(f"CIDR Block '{self.cidr_block}' on Network {self.name} is not valid: {e}")
		return str(next(block.subnets(new_prefix=SUBNET_PREFIX)))

	@property
	def proxy_security_group_id_list(self):
		"""proxy_security_group_ids as a list, for a Gateway Server box."""
		return parse_security_group_ids(self.proxy_security_group_ids)

	@property
	def inference_security_group_id_list(self):
		"""inference_security_group_ids as a list, for an Inference Server box."""
		return parse_security_group_ids(self.inference_security_group_ids)

	@property
	def state_store_security_group_id_list(self):
		"""state_store_security_group_ids as a list, for a Gateway State Store box."""
		return parse_security_group_ids(self.state_store_security_group_ids)

	@property
	def cloud_client(self):
		if not self.cloud_provider:
			frappe.throw(f"Network {self.name} has no Cloud Provider set.")

		provider = frappe.get_doc("Cloud Provider", self.cloud_provider)
		secret = provider.get_password("api_key", raise_exception=False)
		if not (provider.access_key_id and secret):
			frappe.throw(f"Cloud Provider {provider.name} has no credentials set.")
		if not self.region:
			frappe.throw(f"Network {self.name} has no Region set.")

		return build_cloud_client(provider.provider_type, provider.access_key_id, secret, self.region)

	@frappe.whitelist()
	def create_network(self):
		"""Button: create the VPC and public subnet on AWS — an Internet Gateway route and
		auto-assigned public IPs, so a launched Machine is reachable over SSH — then its security
		groups, so one click gets a Network fully ready."""
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
		frappe.msgprint(f"VPC and subnet created for {self.name}.", alert=True)
		self.create_security_groups()

	@frappe.whitelist()
	def create_security_groups(self):
		"""Button: create the Proxy and Inference security groups with their fixed ingress rules.
		Skips a role whose field is already set, so re-clicking never creates a duplicate.

		Only ever creates — what those groups allow on 443 is sync_inference_ingress's, not
		fixed at creation."""
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
				f"{self.name}-inference", "Grove-managed: SSH + engine proxy (80, 443)", self.vpc_id
			)
			self.cloud_client.authorize_ingress(inference_sg_id, INFERENCE_BASE_INGRESS_RULES)
			self.db_set("inference_security_group_ids", inference_sg_id)

		if not self.state_store_security_group_ids:
			store_sg_id = self.cloud_client.create_security_group(
				f"{self.name}-store", "Grove-managed: SSH + redis (6379)", self.vpc_id
			)
			# SSH only, like an inference box: 6379's sources are the gateways, reconciled below.
			self.cloud_client.authorize_ingress(store_sg_id, INFERENCE_BASE_INGRESS_RULES)
			self.db_set("state_store_security_group_ids", store_sg_id)

		frappe.msgprint(f"Security groups created for {self.name}.", alert=True)
		# Straight after creation, so a new group is never briefly open to the world.
		self.sync_inference_ingress()

	@frappe.whitelist()
	def sync_inference_ingress(self):
		"""Button + provision step: make the box's front ports reachable from the proxy fleet and
		the metrics agents, and from nowhere else.

		Both 80 and 443, to the same sources — the box's nginx fronts every engine and both
		exporters either way, so those ports stay on loopback and need no hole of their own.

		RECONCILES rather than adds: a proxy back on a new address leaves its old /32 behind, and
		a pre-existing 0.0.0.0/0 is closed here. Port 22 is deliberately untouched — Ansible
		reaches these boxes from wherever bench runs.

		A store's 6379 is reconciled the same way, to this Network's gateways."""
		if not (self.inference_security_group_ids or self.state_store_security_group_ids):
			frappe.msgprint(f"Network {self.name} has no inference or state store security group.")
			return None
		client = self.cloud_client
		results = {}
		if self.inference_security_group_ids:
			results["inference"] = reconcile_ingress(
				client, self.inference_security_group_id_list, FRONT_PORTS, self.inference_ingress_cidrs
			)
		if self.state_store_security_group_ids:
			results["state_store"] = reconcile_ingress(
				client, self.state_store_security_group_id_list, (REDIS_PORT,), self.state_store_ingress_cidrs
			)
		return results

	@property
	def inference_ingress_cidrs(self):
		"""The addresses that may reach an inference box on this Network's front ports, read live."""
		proxies = frappe.get_all("Gateway Server", fields=["public_ip", "status"])
		agents = _with_machine(frappe.get_all("Monitoring Agent", fields=["machine", "public_ip", "status"]))
		ingresses = _with_machine(frappe.get_all("Ingress Server", fields=["machine", "status"]))
		return inference_ingress_cidrs(proxies, agents, ingresses, self.name)

	@property
	def state_store_ingress_cidrs(self):
		"""The gateways that may reach this Network's store on 6379, read live."""
		gateways = _with_machine(frappe.get_all("Gateway Server", fields=["machine", "status"]))
		return state_store_ingress_cidrs(gateways, self.name)


def reconcile_ingress(client, group_ids, ports, cidrs):
	"""Make each port on these groups allow exactly `cidrs`, and say what moved."""
	changes = [client.sync_ingress(group_id, port, cidrs) for group_id in group_ids for port in ports]
	opened = sorted({cidr for change in changes for cidr in change["opened"]})
	closed = sorted({cidr for change in changes for cidr in change["closed"]})
	frappe.msgprint(
		f"Ports {', '.join(str(port) for port in ports)} now allow {', '.join(cidrs) or 'nothing'}."
		+ (f"<br>Opened: {', '.join(opened)}." if opened else "")
		+ (f"<br>Closed: {', '.join(closed)}." if closed else "")
	)
	return {"allowed": cidrs, "opened": opened, "closed": closed}


def _with_machine(rows):
	"""Each row's Machine joined on, read live rather than mirrored onto the server doc, where the
	copy is only as fresh as that doc's last save."""
	machines = {
		machine["name"]: machine
		for machine in frappe.get_all(
			"Machine",
			filters={"name": ("in", [row["machine"] for row in rows if row.get("machine")])},
			fields=["name", "network", "private_ip"],
		)
	} if rows else {}
	return [{**row, **machines.get(row.get("machine"), {})} for row in rows]


def inference_ingress_cidrs(proxies, agents, ingresses, network):
	"""Every address allowed to reach an inference box in `network` on its front ports, as /32s.

	Three callers and no others: a gateway, an ingress, and the metrics agent. The control plane is
	not one — it reaches a box over SSH and a server at its own admin URL.

	Each contributes the address the box will actually SEE it arrive from, which is why the two
	proxies differ. A gateway dials the box's public IP, so same-VPC traffic still leaves through
	the internet gateway and arrives from the public side; narrowing it to a private /32 while
	engine_url says https://<public ip> shuts the fleet out. An ingress dials privately or not at
	all, so a public /32 would open a hole nothing arrives through.

	Only ingresses whose BOX is in this Network: two VPCs can carve the same 10.x range, so one
	from another might name a different machine entirely. Every agent counts, though — an agent
	box is Grove's own, and the join that would narrow it is not worth the code.

	A Terminated box is gone, and its address belongs to whoever AWS hands it to next."""
	live = lambda rows: [row for row in rows if row.get("status") != GONE_STATUS]  # noqa: E731
	addresses = [proxy.get("public_ip") for proxy in live(proxies)]
	addresses += [reachable_ip({**agent, "ip": agent.get("public_ip")}, network) for agent in live(agents)]
	addresses += [
		ingress.get("private_ip") for ingress in live(ingresses) if ingress.get("network") == network
	]
	return sorted({f"{address}/32" for address in addresses if address})


def state_store_ingress_cidrs(gateways, network):
	"""Every address allowed to reach a store in `network` on 6379, as /32s: its own gateways, by
	the private address they dial it from. Another VPC's 10.x may name a different box, and a
	Terminated gateway's address is AWS's to hand out again."""
	return sorted({
		f"{gateway['private_ip']}/32"
		for gateway in gateways
		if gateway.get("network") == network and gateway.get("private_ip")
		and gateway.get("status") != GONE_STATUS
	})


def sync_fleet_ingress():
	networks = frappe.get_all(
		"Network",
		or_filters={
			"inference_security_group_ids": ("is", "set"),
			"state_store_security_group_ids": ("is", "set"),
		},
		pluck="name",
	)
	for name in networks:
		frappe.enqueue_doc("Network", name, "sync_inference_ingress", queue="short")


def parse_security_group_ids(raw):
	"""A comma-separated security_group_ids field into a list, blanks and whitespace stripped."""
	return [group.strip() for group in (raw or "").split(",") if group.strip()]
