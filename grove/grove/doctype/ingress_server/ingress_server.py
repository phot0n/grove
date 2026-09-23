# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove.fleet import PathwayHost, gateway_agent_release, gateway_agent_version
from grove.grove.doctype.network.network import sync_fleet_ingress


class IngressServer(PathwayHost, Document):
	"""One VPC's front door: the gateways dial it by name over a verified certificate, and it dials
	the replicas in its own Network privately. It holds no tenant state — no keys, users, groups,
	usage or catalog — which is the security payoff of the split, and why this is a doctype of its
	own rather than a Gateway Server with a role flag."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		admin_url: DF.Data | None
		agent_version: DF.Data | None
		data_token: DF.Password | None
		frappe_public_key: DF.Code | None
		geography: DF.Link | None
		is_in_maintenance: DF.Check
		machine: DF.Link
		monitoring_agent: DF.Link | None
		network: DF.Link
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	def before_insert(self):
		super().before_insert()
		# Generated rather than typed: both fields are read-only, and the agent refuses to start
		# without a token. Two separate secrets, never one — admin_token is the control plane's
		# credential, data_token is what every gateway holds.
		if not self.admin_token:
			self.admin_token = frappe.generate_hash(length=48)
		if not self.data_token:
			self.data_token = frappe.generate_hash(length=48)

	def validate(self):
		self.set_admin_url()

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Terminated":
			self.remove_dns_records()
		# This ingress's private address is one of the fleet addresses an inference box opens its
		# front to, so arriving, moving or dying changes what those groups must allow.
		if self.has_value_changed("status") or self.has_value_changed("machine"):
			sync_fleet_ingress()

	def on_trash(self):
		# While its name still says which records are its own.
		self.remove_dns_records()
		# Enqueued, so it recomputes after this delete commits and without this ingress.
		sync_fleet_ingress()

	@property
	def archive_blockers(self):
		"""A box routed through this ingress would lose the private path its gateways dial."""
		boxes = frappe.get_all(
			"Inference Server",
			filters={"ingress": self.name, "status": ("!=", "Terminated")},
			pluck="name",
		)
		if not boxes:
			return []
		return [
			f"Inference Servers still routed through this ingress: {', '.join(boxes)}. "
			"Point them at another ingress, or clear the field, first."
		]

	@frappe.whitelist()
	def sync_replicas(self):
		"""Button: push this ingress's replica table now — every Active replica it owns, dialled
		privately.

		Through full_sync rather than straight at the agent, so the push lands on a Pathway Sync
		doc like every other. A button that reports "queued" and then leaves no record of whether
		it worked is the one you end up debugging by ssh."""
		frappe.enqueue(
			"grove.pathway_sync.full_sync",
			queue="short",
			proxies=[],
			ingresses=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Replica table queued for {self.name} — watch its Pathway Sync.", alert=True)

	@frappe.whitelist()
	def setup(self):
		"""Provision this ingress (OpenResty + Redis + the agent) via ingress.yml."""
		frappe.enqueue_doc(self.doctype, self.name, "provision", queue="long", timeout=1800)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.", alert=True)

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
		self.record_agent_version(rc)
		frappe.db.commit()

		if rc == 0:
			# Its own name has to resolve before the tick pushes to admin_url, which is that name
			# the moment a zone is set.
			self.sync_dns_records()
			# provision writes status through db.set_value, so on_update never fires here — this
			# is what lets a new ingress be let through to an engine.
			sync_fleet_ingress()
		return play_name, rc

	def provision_variables(self, settings):
		"""Everything ingress.yml needs. No tenant variables of any kind pass through here, and
		that absence is the point — see test_ingress_server."""
		return {
			"admin_token": self.get_password("admin_token"),
			"data_token": self.get_password("data_token"),
			**gateway_agent_release(),
			"ingress_id": self.short_name,
			"ingress_hostname": self.hostname,
			# nginx.conf declares a metrics server on :443 — grove_https puts the certificate and
			# the htpasswd it reads on the box before OpenResty is asked to start.
			**settings.scrape_auth_variables,
			# The names this box answers to and its geography's wildcard. Blank zone renders the
			# pre-TLS config, exactly as it does for a gateway.
			**self.tls_variables,
			**self.config_variables,
		}

	@frappe.whitelist()
	def deploy_agent(self):
		"""Button: install the pinned agent release and deploy just it (copy + service restart) to
		this already-provisioned ingress."""
		frappe.enqueue_doc(self.doctype, self.name, "_deploy_agent", queue="long", timeout=1200)
		frappe.msgprint(
			f"Deploying agent {gateway_agent_version()} to {self.name} — watch its Ansible Plays.",
			alert=True,
		)

	@failure.reports_failure(mark_broken=False)
	def _deploy_agent(self, **play):
		"""Install the pinned agent release on the box and rewrite both halves of its configuration.
		`play` names the doc the play is run for, e.g. a Pathway Update.

		Resolved here rather than at enqueue: the certificate key would otherwise be serialised into
		the job payload and sit in Redis. Dropped entirely — agent.env names the certificate's PATH,
		which is a role default, and deploy_tls owns writing the material itself."""
		settings = frappe.get_single("Grove Settings")
		tls_variables = self.tls_variables
		tls_variables.pop("fleet_tls_key", None)
		play_name, rc = self.run_playbook(
			"deploy_agent.yml",
			extravars={
				**gateway_agent_release(),
				"admin_token": self.get_password("admin_token"),
				"data_token": self.get_password("data_token"),
				"ingress_id": self.short_name,
				"ingress_hostname": self.hostname,
				**tls_variables,
				**settings.scrape_auth_variables,
				**self.config_variables,
			},
			**play,
		)
		self.record_agent_version(rc)
		return play_name, rc
