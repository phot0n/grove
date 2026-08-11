# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove import agent_sync
from grove.cloud_provider.route53 import Route53Error
from grove.fleet import FleetHost
from grove.grove.doctype.network.network import sync_fleet_ingress
from grove.naming import GeneratedName
from grove.utils import gateway_service_source


class IngressServer(GeneratedName, FleetHost, Document):
	"""One VPC's front door: the gateways dial it by name over a verified certificate, and it
	dials the replicas in its own Network privately. It holds no tenant state — no keys, users,
	groups, usage or catalog — which is the whole security payoff of the split, and why this is a
	doctype of its own rather than a Gateway Server with a role flag."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		admin_url: DF.Data | None
		data_token: DF.Password | None
		machine: DF.Link
		monitoring_agent: DF.Link | None
		network: DF.Link
		public_ip: DF.Data | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	# Its name is GROVE_INGRESS_ID and its record under the fleet zone, which is the only address a gateway reaches it at.
	name_prefix = "ing"

	def before_insert(self):
		super().before_insert()
		# Generated rather than typed: both fields are read-only, the agent refuses to start
		# without a token, and a provision that fails on a blank one is a wasted twenty-minute
		# play. Two separate secrets, never one: admin_token is the control plane's credential
		# and data_token is what every gateway holds.
		if not self.admin_token:
			self.admin_token = frappe.generate_hash(length=48)
		if not self.data_token:
			self.data_token = frappe.generate_hash(length=48)

	def validate(self):
		self.set_admin_url()
		self.validate_machine_network()

	def validate_machine_network(self):
		"""The Network on this doc has to be the one its BOX is in.

		Nothing downstream can catch a mismatch: the ingress would be given that Network's
		replicas, be unable to reach any of them, and be left out of their security group — three
		silences instead of one error. It also decides which boxes trust this ingress's private
		address, and two VPCs can carve the same 10.x range."""
		if not (self.network and self.machine):
			return
		box_network = frappe.db.get_value("Machine", self.machine, "network")
		if box_network != self.network:
			frappe.throw(
				f"Machine {self.machine} is in Network {box_network or 'none'}, but this ingress "
				f"says {self.network}. An ingress fronts the VPC its own box sits in."
			)

	def on_update(self):
		# A newly-Active ingress has an empty replica table until something fills it, and the
		# scheduled run only ticks when a deployment moved.
		if self.has_value_changed("status") and self.status == "Active" and self.admin_url:
			frappe.enqueue(
				"grove.agent_sync.full_sync",
				queue="short",
				proxies=[],
				ingresses=[self.name],
				trigger="Ingress Activated",
			)
		if self.has_value_changed("status") and self.status == "Terminated":
			self.remove_dns_records()
		# An inference box only opens its front to addresses in the fleet, and this ingress's
		# private address is one of them — so an ingress that arrived, moved or died changes what
		# those groups must allow.
		if self.has_value_changed("status") or self.has_value_changed("machine"):
			sync_fleet_ingress()

	def on_trash(self):
		# Before the doc goes, while its name still says which records are its own.
		self.remove_dns_records()
		# Enqueued, so it recomputes after this delete commits and without this ingress in the set.
		sync_fleet_ingress()

	@frappe.whitelist()
	def sync_dns_records(self):
		"""Button + provision step: point this box's own name at it. UPSERT, so a box that came
		back on a new address is corrected by running it again.

		One record and no shared name: a gateway reaches this ingress out of its route table, not
		by resolving something that names several."""
		client, settings = self.dns_client()
		if not client:
			return None
		return client.upsert_ingress_records(
			settings.fleet_zone, self.hostname, self.public_ip, self.name
		)

	def remove_dns_records(self):
		"""This box's record, on the way out.

		A record that is already gone is not an error worth blocking a deletion over; AWS says so
		with InvalidChangeBatch, and only that code is tolerated."""
		if not self.has_dns_records:
			return None
		client, settings = self.dns_client()
		if not client:
			return None
		try:
			return client.delete_ingress_records(
				settings.fleet_zone, self.hostname, self.public_ip, self.name
			)
		except Route53Error as e:
			if e.code != "InvalidChangeBatch":
				raise
			return None

	@frappe.whitelist()
	def sync_replicas(self):
		"""Button: push this ingress's replica table now — every Active replica it owns, dialled
		privately.

		Through full_sync rather than straight at the agent, so the push lands on a Agent Sync
		doc like every other. A button that reports "queued" and then leaves no record of whether
		it worked is the one you end up debugging by ssh."""
		frappe.enqueue(
			"grove.agent_sync.full_sync",
			queue="short",
			proxies=[],
			ingresses=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Replica table queued for {self.name} — watch its Agent Sync.")

	@frappe.whitelist()
	def setup(self):
		"""Provision this ingress (OpenResty + Redis + the agent) via ingress.yml."""
		frappe.enqueue_doc(self.doctype, self.name, "provision", queue="long", timeout=1800)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.")

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		"""Run ingress.yml against this box's Machine. On success, mark Active and put it in DNS —
		nothing resolves either of its names until that runs."""
		frappe.db.set_value("Ingress Server", self.name, "status", "Installing")
		frappe.db.commit()

		settings = frappe.get_single("Grove Settings")
		play_name, rc = self.run_playbook("ingress.yml", extravars=self.provision_variables(settings))

		# admin_url is derived at validate, and a zone set since this box was last saved leaves it
		# naming the old address.
		self.set_admin_url()
		frappe.db.set_value(
			"Ingress Server",
			self.name,
			{"status": "Active" if rc == 0 else "Broken", "admin_url": self.admin_url},
		)
		frappe.db.commit()

		if rc == 0:
			# DNS before the table: the push goes to admin_url, which is this box's own name the
			# moment a zone is set, and nothing resolves it until this runs.
			self.sync_dns_records()
			# provision writes status through db.set_value, so on_update never fires here — these
			# two are what let a new ingress reach an engine and be let through to one.
			sync_fleet_ingress()
			agent_sync.full_sync(proxies=[], ingresses=[self.name], trigger="Provision")
		return play_name, rc

	def provision_variables(self, settings):
		"""Everything ingress.yml needs. No tenant variables of any kind pass through here, and
		that absence is the point — see test_ingress_server."""
		return {
			"admin_token": self.get_password("admin_token"),
			"data_token": self.get_password("data_token"),
			"agent_source": gateway_service_source(),
			"ingress_id": self.name,
			"ingress_hostname": self.hostname,
			# nginx.conf declares a metrics server on :443 — grove_https puts the certificate and
			# the htpasswd it reads on the box before OpenResty is asked to start.
			**settings.scrape_auth_variables,
			# The names this box answers to and the wildcard both of them share. Blank zone
			# renders the pre-TLS config, exactly as it does for a gateway.
			**settings.tls_variables,
		}

	@frappe.whitelist()
	def deploy_agent(self):
		"""Button: build the latest agent binary and deploy just it (copy + service restart) to
		this already-provisioned ingress."""
		frappe.enqueue_doc(self.doctype, self.name, "_deploy_agent", queue="long", timeout=1200)
		frappe.msgprint(f"Building + deploying latest agent to {self.name} — watch its Ansible Plays.")

	@failure.reports_failure(mark_broken=False)
	def _deploy_agent(self):
		return self.run_playbook(
			"deploy_agent.yml",
			extravars={
				"agent_source": gateway_service_source(),
				"admin_token": self.get_password("admin_token"),
				"data_token": self.get_password("data_token"),
				"ingress_id": self.name,
			},
		)

	@frappe.whitelist()
	def deploy_openresty(self):
		"""Button: push the nginx.conf this bench has, validate, graceful reload. Config only, so
		Redis keeps its replica table and the agent is not restarted."""
		frappe.enqueue_doc(self.doctype, self.name, "_deploy_openresty", queue="long", timeout=900)
		frappe.msgprint(f"Deploying OpenResty config to {self.name} — watch its Ansible Plays.")

	def _deploy_openresty(self):
		"""Resolved here, not at enqueue: the scrape password hash would otherwise be serialised
		into the job payload and sit in Redis."""
		settings = frappe.get_single("Grove Settings")
		# nginx.conf keys its shape on the certificate, so that one is here — but the key only
		# feeds fleet_tls, which deploy_tls owns. Dropping it skips that role and keeps the
		# fleet's private key out of a config push entirely.
		tls_variables = settings.tls_variables
		tls_variables.pop("fleet_tls_key", None)
		return self.run_playbook(
			"deploy_openresty.yml",
			extravars={
				"ingress_hostname": self.hostname,
				**settings.scrape_auth_variables,
				**tls_variables,
			},
		)
