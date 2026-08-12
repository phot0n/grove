# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove import agent_sync
from grove.cloud_provider.route53 import Route53Error
from grove.fleet import GATEWAY_AGENT_VERSION, FleetHost
from grove.grove.doctype.network.network import sync_fleet_ingress
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
	dns_settings = ("fleet_zone", "gateway_host", "dns_provider")
	dns_fields = ("public_ip", "region")

	def validate(self):
		self.set_admin_url()
		self.set_admin_token()
		self.validate_region_is_free()

	def validate_region_is_free(self):
		"""One gateway per region, because Route53 allows exactly one.

		A latency record set is keyed on (name, type, REGION) — SetIdentifier names the row but does
		not make two rows in the same region distinct. A second gateway there is refused by AWS with
		`InvalidChangeBatch ... a latency RRSet with the same name, type and region already exists`,
		twenty minutes into a provision, after the binary has been built.

		Checked only when the region CHANGES, and only on a fleet that has DNS configured. Both
		matter: validating unconditionally would block every unrelated edit to a box that is already
		in a conflicting state — including the edit that fixes it — and a fleet with no zone writes
		no records at all, so it has no constraint to honour.

		Terminated boxes are ignored: their records are removed on the way out, so their region is
		free."""
		if not self.region or not self.has_value_changed("region"):
			return
		settings = frappe.get_single("Grove Settings")
		if not all(settings.get(field) for field in self.dns_settings):
			return
		clash = frappe.db.get_value(
			"Gateway Server",
			{"region": self.region, "status": ("!=", "Terminated"), "name": ("!=", self.name)},
			"name",
		)
		if clash:
			frappe.throw(
				f"{clash} is already the gateway for {self.region}, and Route53 allows one latency "
				f"record per region — a second one there is refused by AWS. Give this box a "
				f"different Region, or terminate {clash} first."
			)

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
		# A newly-Active proxy needs the full current state now — the background
		# job only pushes dirty deltas, so full-sync this one immediately.
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
	def full_sync(self):
		"""Button: push the COMPLETE key set + routing table to this proxy now
		(logged on a Agent Sync doc)."""
		frappe.enqueue(
			"grove.agent_sync.full_sync",
			queue="short",
			proxies=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Full sync queued for {self.name}.")

	@frappe.whitelist()
	def sync_dns_records(self):
		"""Button + provision step: point this box's own name at it, and add it to the Gateway
		Host latency set so clients nearest this region resolve here. UPSERT, so a box that came
		back on a new address is corrected by running it again."""
		client, settings = self.dns_client()
		if not client:
			return None
		return client.upsert_gateway_records(
			settings.fleet_zone,
			self.hostname,
			settings.gateway_host,
			self.public_ip,
			self.region,
			self.name,
		)

	def remove_dns_records(self):
		"""Both of this box's records, on the way out. A DELETE has to repeat the record exactly
		as it was written, so this passes the same values the upsert did.

		A record that is already gone is not an error worth blocking a deletion over — AWS says
		so with InvalidChangeBatch, and only that code is tolerated."""
		if not self.has_dns_records:
			return None
		client, settings = self.dns_client()
		if not client:
			return None
		try:
			return client.delete_gateway_records(
				settings.fleet_zone,
				self.hostname,
				settings.gateway_host,
				self.public_ip,
				self.region,
				self.name,
			)
		except Route53Error as e:
			if e.code != "InvalidChangeBatch":
				raise
			return None

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
		frappe.msgprint(f"Deploying agent {GATEWAY_AGENT_VERSION} to {self.name} — watch its Ansible Plays.")

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
				"agent_version": GATEWAY_AGENT_VERSION,
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
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.")

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
				"agent_version": GATEWAY_AGENT_VERSION,
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
