# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove.server import Server

REDIS_PORT = 6379
# What a gateway with no store in its Network runs on: its own Redis.
LOOPBACK_REDIS = {"redis_addr": f"127.0.0.1:{REDIS_PORT}", "redis_password": "", "redis_shared": False}


class GatewayStore(Server, Document):
	"""The one Redis a Network's gateways share. One in-flight counter per replica is what caps a
	standalone box across those gateways; everything else they hold lives here with it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		frappe_public_key: DF.Code | None
		geography: DF.Link | None
		machine: DF.Link
		network: DF.Link | None
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		redis_password: DF.Password | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	def validate(self):
		self.validate_one_per_network()
		self.set_redis_password()

	def validate_one_per_network(self):
		"""Two stores would split a Network's gateways onto two counters — the over-admission a
		store exists to remove."""
		network = frappe.db.get_value("Machine", self.machine, "network")
		if not network:
			frappe.throw(f"Machine {self.machine} is in no Network — a store serves one Network's gateways.")
		if self.status == "Terminated":
			return
		others = [name for name in stores_in(network, status=("!=", "Terminated")) if name != self.name]
		if others:
			frappe.throw(f"Network {network} already has Gateway Store {others[0]}.")

	def set_redis_password(self):
		"""Tested through get_password: a saved Password field reads back as asterisks even when
		the value behind it is gone."""
		if not self.get_password("redis_password", raise_exception=False):
			self.redis_password = frappe.generate_hash(length=48)

	@property
	def archive_blockers(self):
		"""A gateway on this store authenticates nothing without it."""
		gateways = frappe.get_all(
			"Gateway Server",
			filters={"gateway_store": self.name, "status": ("!=", "Terminated")},
			pluck="name",
		)
		if not gateways:
			return []
		return [f"Gateways still run on this store: {', '.join(gateways)}."]

	@property
	def listen_ip(self):
		"""The Machine's private address, live: where Redis binds and the gateways dial."""
		private_ip = frappe.db.get_value("Machine", self.machine, "private_ip")
		if not private_ip:
			frappe.throw(f"Machine {self.machine} has no private IP — its gateways would have nothing to dial.")
		return private_ip

	@property
	def redis_variables(self):
		"""What a gateway on this store is given. Carries the password, so resolve it inside a job."""
		return {
			"redis_addr": f"{self.listen_ip}:{REDIS_PORT}",
			"redis_password": self.get_password("redis_password"),
			"redis_shared": True,
		}

	@frappe.whitelist()
	def setup(self):
		"""Button: install Redis on this store's box."""
		frappe.enqueue_doc(self.doctype, self.name, "provision", queue="long", timeout=1800)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		frappe.db.set_value(self.doctype, self.name, "status", "Installing")
		frappe.db.commit()
		play_name, rc = self.run_playbook(
			"store.yml",
			extravars={
				"redis_bind_ip": self.listen_ip,
				"redis_password": self.get_password("redis_password"),
			},
		)
		frappe.db.set_value(self.doctype, self.name, "status", "Active" if rc == 0 else "Broken")
		return play_name, rc


def stores_in(network, **filters):
	"""The stores whose Machine is in `network`, membership read off the Machine live."""
	boxes = frappe.get_all("Machine", filters={"network": network}, pluck="name")
	if not boxes:
		return []
	return frappe.get_all(
		"Gateway Store", filters={"machine": ("in", boxes), **filters}, pluck="name"
	)


def store_writers(store):
	"""The store's Active writers, in the order a sync tries them."""
	return frappe.get_all(
		"Gateway Server",
		filters={"gateway_store": store, "is_store_writer": 1, "status": "Active"},
		order_by="name asc",
		pluck="name",
	)


def gateway_redis_variables(store):
	"""A gateway's Redis: the store's when it has one, else its own on loopback."""
	if not store:
		return dict(LOOPBACK_REDIS)
	return frappe.get_doc("Gateway Store", store).redis_variables
