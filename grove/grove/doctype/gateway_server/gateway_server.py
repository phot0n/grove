# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove import agent_sync
from grove.cloud_provider.route53 import Route53Error, group_name
from grove.fleet import (
	GATEWAY_DNS_SETTINGS,
	FleetHost,
	gateway_agent_version,
	gateway_health_checks_enabled,
	gateway_latency_routing_enabled,
)
from grove.grove.doctype.network.network import sync_fleet_ingress
from grove.grove.doctype.region.region import dns_label, sync_region_dns
from grove.naming import GeneratedName


class GatewayServer(GeneratedName, FleetHost, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		admin_url: DF.Data | None
		agent_version: DF.Data | None
		health_check_id: DF.Data | None
		is_static_ip: DF.Check
		machine: DF.Link
		monitoring_agent: DF.Link | None
		public_ip: DF.Data | None
		redis_appendfsync: DF.Literal["always", "everysec"]
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	# Its name is GROVE_GATEWAY_ID, its record under the fleet zone, and the first part of every request id it stamps.
	name_prefix = "gw"

	# The gateway's own name is GROVE_GATEWAY_ID and the first part of every request id it
	# stamps; its records also need Gateway Host to belong to and a Region to be sorted by.
	dns_settings = GATEWAY_DNS_SETTINGS
	dns_fields = ("public_ip", "region")

	def validate(self):
		self.set_admin_url()
		self.set_admin_token()

	def set_admin_token(self):
		"""The credential the control plane authenticates every push with.

		Generated rather than typed: the field is read-only, so there was no way to enter one in the
		UI at all — a Gateway Server was created with a blank token and every path that needed one
		raised "Password not found", including a twenty-minute provision that got as far as building
		the binary before it failed. The Ingress Server has always minted its own; this is the same
		credential and had no business behaving differently.

		In validate rather than before_insert, so it also heals the docs that already exist without
		one. Clearing the field and saving is how it is rotated — which then needs a Deploy Agent,
		because the box holds the old value until agent.env is rewritten.

		Tested through get_password, NOT `if not self.admin_token`. A saved Password field puts a
		row of asterisks in the doc's own column and the real value in __Auth, so the field reads
		back truthy even when the actual secret is gone — which is precisely the state a doc lands
		in if its __Auth row is lost, and precisely the state this is here to fix."""
		if not self.get_password("admin_token", raise_exception=False):
			self.admin_token = frappe.generate_hash(length=48)

	def on_update(self):
		# A newly-Active proxy needs the full current state now, not on the next tick.
		if self.has_value_changed("status") and self.status == "Active" and self.admin_url:
			frappe.enqueue(
				"grove.agent_sync.full_sync",
				queue="short",
				proxies=[self.name],
				trigger="Proxy Activated",
			)
		if self.has_value_changed("status") and self.status == "Terminated":
			self.remove_dns_records()
		# An inference box only answers on 443 to addresses in the proxy fleet, so a proxy that
		# arrived, moved or died changes what those groups must allow.
		if self.has_value_changed("public_ip") or self.has_value_changed("status"):
			sync_fleet_ingress()

	def on_trash(self):
		# Before the doc goes, while its name still says which records are its own. A row left
		# behind in the Gateway Host latency set is a black hole for whichever share of customers
		# resolves to it.
		self.remove_dns_records()
		# Enqueued, so it recomputes after this delete commits and without this proxy in the set.
		sync_fleet_ingress()

	@frappe.whitelist()
	def check_state(self):
		"""Button: diff this box's stored state hashes against the current desired state,
		pushing nothing. Says in-sync, or which sections a tick would push."""
		import requests

		try:
			result = agent_sync.check_state("Gateway Server", self.name)
		except requests.RequestException as e:
			frappe.throw(f"Could not reach {self.name}: {e}")
		if result["in_sync"]:
			frappe.msgprint(f"{self.name} holds the current desired state.", alert=True)
		else:
			frappe.msgprint(
				f"Drift on {self.name}: {', '.join(result['drift'])} — "
				"the next tick (or Full Sync) will push it."
			)
		return result

	@frappe.whitelist()
	def full_sync(self):
		"""Button: push the COMPLETE key set + routing table to this proxy now
		(logged on a Agent Sync doc)."""
		frappe.enqueue(
			"grove.agent_sync.full_sync",
			queue="short",
			proxies=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Full sync queued for {self.name}.", alert=True)

	@property
	def caller_reference(self):
		"""Idempotency token for this box's health check. The creation stamp is in it so a name
		handed out again after a terminate cannot collide with the old box's check."""
		return f"grove-{self.name}-{frappe.utils.get_datetime(self.creation):%Y%m%d%H%M%S}"

	def ensure_health_check(self, client):
		"""This box's own Route53 health check, created once — and nothing at all on a fleet that has
		health checking off. Its id is what the box's multivalue row carries and what its region's
		calculated check counts as a child, so a box without one is permanently healthy to both, which
		is exactly what a development fleet wants."""
		if not gateway_health_checks_enabled():
			return ""
		if not self.health_check_id:
			self.db_set(
				"health_check_id",
				client.create_endpoint_health_check(self.public_ip, self.hostname, self.caller_reference),
			)
		return self.health_check_id

	def set_name(self, gateway_host):
		"""The multivalue set this box's row belongs in: its region's, under latency routing, or the
		shared name itself when there is no region tier."""
		if not gateway_latency_routing_enabled():
			return gateway_host
		return group_name(gateway_host, dns_label(self.region))

	@frappe.whitelist()
	def sync_dns_records(self):
		"""Button + provision step: point this box's own name at it, put it in the multivalue set it
		belongs to behind its own health check, and reconcile the region tier around it. UPSERT, so a
		box that came back on a new address is corrected by running it again."""
		client, settings = self.dns_client()
		if not client:
			return None
		health_check_id = self.ensure_health_check(client)
		# The region's row is written AFTER this box joins its set, because it points at a set that
		# would otherwise be empty — but it has to be GONE BEFORE, in simple mode: it is a CNAME at
		# the shared name, and Route53 refuses a CNAME beside a record of any other type, which is
		# exactly what this box is about to write there.
		latency = gateway_latency_routing_enabled()
		if not latency:
			sync_region_dns(self.region)
		change = client.upsert_gateway_records(
			settings.fleet_zone,
			self.hostname,
			settings.gateway_host,
			self.set_name(settings.gateway_host),
			self.public_ip,
			self.name,
			health_check_id,
		)
		if latency:
			sync_region_dns(self.region)
		# Health checking was turned off since this box was last synced. Its row and its region's
		# check have both let go by now, so the check it used to name can be released — it would
		# otherwise sit billed and watching nothing.
		if not health_check_id:
			self.delete_health_check(client)
		return change

	def delete_health_check(self, client):
		"""Drop this box's check and forget it. Only ever after its own row and its region's
		calculated check have let go — Route53 refuses to delete a check anything still names."""
		if self.health_check_id:
			client.delete_health_check(self.health_check_id)
			self.db_set("health_check_id", "")

	def remove_dns_records(self):
		"""This box's two records, then its region's tier, then its health check — in that order,
		because Route53 refuses to delete a check a record set or a calculated check still names.

		A record that is already gone is not an error worth blocking a deletion over — AWS says
		so with InvalidChangeBatch, and only that code is tolerated."""
		if not self.has_dns_records:
			return None
		client, settings = self.dns_client()
		if not client:
			return None
		change = None
		try:
			change = client.delete_gateway_records(
				settings.fleet_zone,
				self.hostname,
				self.set_name(settings.gateway_host),
				self.public_ip,
				self.name,
				self.health_check_id,
			)
		except Route53Error as e:
			if e.code != "InvalidChangeBatch":
				raise
		# Excluded by name: on_trash runs while this doc's row is still in the database, and a region
		# whose last gateway is going has its whole tier removed here.
		sync_region_dns(self.region, exclude=self.name)
		self.delete_health_check(client)
		return change

	@frappe.whitelist()
	def deploy_agent(self):
		"""Button: install the pinned gateway release and deploy just it (copy +
		service restart) to this already-provisioned proxy."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_deploy_agent",
			queue="long",
			timeout=1200,
		)
		frappe.msgprint(
			f"Deploying agent {gateway_agent_version()} to {self.name} — watch its Ansible Plays.",
			alert=True,
		)

	@failure.reports_failure(mark_broken=False)
	def _deploy_agent(self):
		"""Install the pinned agent release on the box and rewrite both halves of its configuration.

		One button for binary AND config, because the gateway has no other config surface: agent.env
		names every listener, certificate and hostname, and config.json holds the tunables. Written
		whole from the same extra-vars provision passes, so a config-only run never blanks the admin
		token.

		Resolved here rather than at enqueue, like the provision path: the certificate key would
		otherwise be serialised into the job payload and sit in Redis. Only the key is dropped —
		agent.env names the certificate's PATH, so the paths' role defaults still have to load, and
		the play includes fleet_tls for exactly that."""
		settings = frappe.get_single("Grove Settings")
		tls_variables = settings.tls_variables
		tls_variables.pop("fleet_tls_key", None)
		play_name, rc = self.run_playbook(
			"deploy_agent.yml",
			extravars={
				"agent_version": gateway_agent_version(),
				"admin_token": self.get_password("admin_token"),
				"gateway_id": self.name,
				# Which routes this gateway prefers: a same-region row wins its tier outright.
				"gateway_region": self.region or "",
				"proxy_hostname": self.hostname,
				**tls_variables,
				**settings.scrape_auth_variables,
				**settings.gateway_variables,
			},
		)
		self.record_agent_version(rc)
		return play_name, rc

	@frappe.whitelist()
	def setup(self):
		"""Provision this proxy (OpenResty + Redis + Go agent) via gateway.yml."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"provision",
			queue="long",
			timeout=1800,
		)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		"""Run gateway.yml against the Gateway Server's Machine → OpenResty + Redis +
		Go agent. On success, mark Active and project keys/routes."""
		frappe.db.set_value("Gateway Server", self.name, "status", "Installing")
		frappe.db.commit()

		settings = frappe.get_single("Grove Settings")
		play_name, rc = self.run_playbook(
			"gateway.yml",
			extravars={
				"admin_token": self.get_password("admin_token"),
				"agent_version": gateway_agent_version(),
				"gateway_id": self.name,
				# Which routes this gateway prefers: a same-region row wins its tier outright.
				"gateway_region": self.region or "",
				"proxy_hostname": self.hostname,
				**settings.gateway_variables,
				# nginx.conf declares a metrics server on :443 — grove_https puts the certificate
				# and the htpasswd it reads on the box before OpenResty is asked to start.
				**settings.scrape_auth_variables,
				# The names this box answers to and the wildcard both of them share. Blank zone
				# renders the pre-TLS config, so a fleet without one provisions as it always did.
				**settings.tls_variables,
			},
		)

		# admin_url is derived at validate, and a zone set after this box was last saved leaves it
		# naming the old address. Refreshed here so the sync below goes where the box now answers.
		self.set_admin_url()
		frappe.db.set_value(
			"Gateway Server",
			self.name,
			{"status": "Active" if rc == 0 else "Broken", "admin_url": self.admin_url},
		)
		self.record_agent_version(rc)
		frappe.db.commit()

		if rc == 0:
			# DNS before the sync: the routes push goes to admin_url, which is this box's own
			# name the moment a zone is set, and nothing resolves it until this runs.
			self.sync_dns_records()
			# provision writes status and public_ip through db.set_value, so on_update never
			# fires here — this is the only thing that lets a new proxy reach an engine.
			sync_fleet_ingress()
			agent_sync.full_sync(proxies=[self.name], trigger="Provision")
		return play_name, rc
