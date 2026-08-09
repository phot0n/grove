# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe.model.document import Document

from grove.cloud_provider.base import CloudClientError, build_cloud_client
from grove.monitoring import BOX_HTTP_PORT, BOX_HTTPS_PORT
from grove.net import reachable_ip
from grove.utils import is_label_under, slugify

# A proxy serves customers on 80/443 and its own admin API on 443 — both from anywhere, since
# neither the client nor the control plane has a pinned address.
PROXY_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"},
]
# What an inference box opens at creation. 443 is NOT here: it carries the engine proxy, which
# only the gateway and the metrics agent ever dial, so its sources are computed per box rather
# than fixed — see inference_ingress_cidrs and sync_inference_ingress.
INFERENCE_BASE_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
]

# Pool auto-assigned CIDR blocks are carved from — /16s never collide with each other, so any
# two Networks can be peered later without an overlap.
CIDR_POOL = ipaddress.ip_network("10.0.0.0/8")
CIDR_PREFIX = 16
# One public subnet per Network, carved off the front of its /16.
SUBNET_PREFIX = 24

# The ports the box's own nginx answers on, both reconciled to the same sources. 80 is where the
# front is going — plain HTTP, reachable only from inside the fleet, so the box needs no
# certificate — and 443 is where it still is. Opening 80 early costs nothing while nothing listens
# on it; 443 comes out once every box has moved.
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
	def ingress_host(self):
		"""The one name every ingress in this Network answers to, and the only ingress address a
		gateway's route table ever holds — so how many ingresses sit behind it is a DNS fact, not
		a control-plane one. Blank with no Fleet Zone set, like every other fleet name.

		Exactly one label under the zone, because the fleet certificate is a wildcard and
		`*.<zone>` covers nothing deeper. A Network named with a dot in it would produce one that
		is not, and an ingress serving a name the certificate does not cover fails as a TLS error
		in the gateway, long after this."""
		zone = frappe.db.get_single_value("Grove Settings", "fleet_zone")
		if not zone:
			return ""
		host = f"{slugify(self.name)}-ingress.{zone}"
		if not is_label_under(host, zone):
			frappe.throw(
				f"Network {self.name} does not make a DNS label — '{host}' is more than one "
				f"label under '{zone}', which the fleet wildcard does not cover."
			)
		return host

	@property
	def proxy_security_group_id_list(self):
		"""proxy_security_group_ids as a list, for a Gateway Server box."""
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

		Only ever creates: a Network whose groups already exist keeps whatever rules they hold.
		What those groups allow on 443 is not fixed at creation anyway — sync_inference_ingress
		owns it, and running that is how a fleet built before this change is brought in line."""
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

		frappe.msgprint(f"Security groups created for {self.name}.")
		# Straight after creation, so a new group is never briefly open to the world on 443.
		self.sync_inference_ingress()

	@frappe.whitelist()
	def sync_inference_ingress(self):
		"""Button + provision step: make the box's front ports on this Network's inference security
		group reachable from the proxy fleet and the metrics agents, and from nowhere else.

		Both 80 and 443, to the same sources. The box's nginx is moving from 443 to 80 — it fronts
		every engine and both exporters either way, so those ports stay on loopback and never need
		a hole of their own. Opening 80 ahead of the move costs nothing while nothing listens on
		it, and means no box is briefly unreachable when it does.

		Reconciles rather than adds: a proxy that came back on a new address leaves its old /32
		behind, and the 0.0.0.0/0 a pre-existing group still carries is closed here. Port 22 is
		deliberately untouched — Ansible reaches these boxes from wherever bench runs."""
		if not self.inference_security_group_ids:
			frappe.msgprint(f"Network {self.name} has no inference security group.")
			return None

		cidrs = self.inference_ingress_cidrs
		client = self.cloud_client
		changes = [
			client.sync_ingress(group_id, port, cidrs)
			for group_id in self.inference_security_group_id_list
			for port in FRONT_PORTS
		]
		opened = sorted({cidr for change in changes for cidr in change["opened"]})
		closed = sorted({cidr for change in changes for cidr in change["closed"]})
		ports = ", ".join(str(port) for port in FRONT_PORTS)
		frappe.msgprint(
			f"Ports {ports} on {self.name} now allow {', '.join(cidrs) or 'nothing'}."
			+ (f"<br>Opened: {', '.join(opened)}." if opened else "")
			+ (f"<br>Closed: {', '.join(closed)}." if closed else "")
		)
		return {"allowed": cidrs, "opened": opened, "closed": closed}

	@property
	def inference_ingress_cidrs(self):
		"""The addresses that may reach an inference box on this Network's 443, read live."""
		proxies = frappe.get_all("Gateway Server", fields=["public_ip", "status"])
		agents = frappe.get_all("Monitoring Agent", fields=["machine", "public_ip", "status"])
		machines = {
			machine["name"]: machine
			for machine in frappe.get_all(
				"Machine",
				filters={"name": ("in", [agent["machine"] for agent in agents if agent["machine"]])},
				fields=["name", "network", "private_ip"],
			)
		}
		agents = [{**agent, **machines.get(agent["machine"], {})} for agent in agents]
		return inference_ingress_cidrs(proxies, agents, self.name)


def inference_ingress_cidrs(proxies, agents, network):
	"""Every address allowed to reach an inference box in `network` on its front ports, as /32s.

	Two callers and no others. The gateway forwards to https://<box public ip>/e/<slug>, and the
	metrics agent scrapes /metrics/node and /metrics/gpu through the same front. The control plane
	is not one of them: it reaches a box over SSH and a proxy at its own admin URL.

	Every source contributes the address the box will actually see it arrive from. An agent goes
	through net.reachable_ip, the same rule it picks its scrape address by. A gateway contributes
	its PUBLIC address unconditionally, and that is not an oversight to tidy up: the gateway dials
	the box's public IP, so same-VPC traffic still leaves through the internet gateway and arrives
	from the public side. The dial address and this rule have to move together — narrowing this to
	a private /32 while engine_url still says https://<public ip> shuts the fleet out.

	Every agent counts, not only those scraping this Network: an agent box is Grove's own, and the
	join that would narrow it is not worth the code.

	A box that is Terminated is gone, and its address belongs to whoever AWS hands it to next."""
	addresses = [proxy.get("public_ip") for proxy in proxies if proxy.get("status") != GONE_STATUS]
	addresses += [
		reachable_ip({**agent, "ip": agent.get("public_ip")}, network)
		for agent in agents
		if agent.get("status") != GONE_STATUS
	]
	return sorted({f"{address}/32" for address in addresses if address})


def sync_fleet_ingress():
	"""Re-reconcile every Network that has an inference security group. Enqueued rather than run
	inline: the proxy change that triggers it must not wait on an AWS call per Network, and one
	unreachable account must not stop the rest."""
	networks = frappe.get_all(
		"Network", filters={"inference_security_group_ids": ("is", "set")}, pluck="name"
	)
	for name in networks:
		frappe.enqueue_doc("Network", name, "sync_inference_ingress", queue="short")


def parse_security_group_ids(raw):
	"""A comma-separated security_group_ids field into a list, blanks and whitespace stripped."""
	return [group.strip() for group in (raw or "").split(",") if group.strip()]
