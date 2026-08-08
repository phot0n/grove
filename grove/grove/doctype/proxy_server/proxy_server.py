# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import gateway_sync
from grove.ansible import AnsibleHost
from grove.cloud_provider.route53 import Route53Client, Route53Error
from grove.grove.doctype.network.network import sync_fleet_ingress
from grove.monitoring import run_exporters_play
from grove.tls import dns_credentials
from grove.utils import gateway_service_source, validate_id_safe_name


class ProxyServer(AnsibleHost, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		admin_url: DF.Data | None
		is_static_ip: DF.Check
		machine: DF.Link
		monitoring_agent: DF.Link | None
		public_ip: DF.Data | None
		redis_appendfsync: DF.Literal["always", "everysec"]
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	def before_insert(self):
		# This name is the gateway's own id (GROVE_GATEWAY_ID, set from proxy.yml) and so the
		# FIRST part of every request id this proxy stamps. Typed by an operator
		# (autoname: prompt), so it is checked here and on rename — the only two moments it can
		# be chosen.
		validate_id_safe_name(self.doctype, self.name)

	def before_rename(self, old_name, new_name, merge=False):
		validate_id_safe_name(self.doctype, new_name)

	def validate(self):
		self.set_admin_url()

	@property
	def hostname(self):
		"""The name that reaches THIS box: <Proxy Server name>.<proxy zone>, covered by the
		fleet's wildcard. Blank with no zone set, and then the box has no name at all — it is
		reached by IP over plain HTTP, which is how every proxy worked before TLS.

		Doc names are already DNS-legal labels: validate_id_safe_name allows letters, digits
		and '-' only, on insert and on rename."""
		zone = frappe.db.get_single_value("Grove Settings", "proxy_zone")
		return f"{self.name}.{zone}" if zone else ""

	def set_admin_url(self):
		"""Where the control plane reaches this box's agent. Derived, never typed: it has to name
		ONE box, and Gateway Host deliberately names all of them at once. Once it is https on a
		name the fleet certificate covers, `requests` verifies it by default — which is the
		entire change needed for gateway_sync and usage_pull to stop trusting the network."""
		if self.hostname:
			self.admin_url = f"https://{self.hostname}/grove-admin"
		elif self.public_ip:
			self.admin_url = f"http://{self.public_ip}/grove-admin"

	def on_update(self):
		# A newly-Active proxy needs the full current state now — the background
		# job only pushes dirty deltas, so full-sync this one immediately.
		if self.has_value_changed("status") and self.status == "Active" and self.admin_url:
			frappe.enqueue(
				"grove.gateway_sync.full_sync",
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
		(logged on a Gateway Sync doc)."""
		frappe.enqueue(
			"grove.gateway_sync.full_sync",
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
		return client.upsert_proxy_records(
			settings.proxy_zone,
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
		client, settings = self.dns_client()
		if not client:
			return None
		try:
			return client.delete_proxy_records(
				settings.proxy_zone,
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

	def dns_client(self):
		"""(Route53Client, Grove Settings) once everything a record needs is set, (None, settings)
		otherwise. Silent rather than loud: a fleet with no zone configured is the pre-TLS setup,
		and provisioning a box there should not start failing."""
		settings = frappe.get_single("Grove Settings")
		if not (settings.proxy_zone and settings.gateway_host and settings.dns_provider):
			return None, settings
		if not (self.public_ip and self.region):
			frappe.throw(f"Proxy Server {self.name} needs a public IP and a Region before its DNS records.")
		return Route53Client(*dns_credentials(settings)), settings

	@frappe.whitelist()
	def deploy_tls(self):
		"""Write the current fleet certificate on this box and reload OpenResty. Nothing else —
		this is what the daily renewal pushes, and it runs against live gateways."""
		return self.run_playbook(
			"deploy_tls.yml", extravars=frappe.get_single("Grove Settings").tls_variables
		)

	@frappe.whitelist()
	def install_exporters(self):
		"""Button: install this box's metrics exporters (long job — it SSHes to the box).
		They only listen; the Monitoring Agent named on this doc is what scrapes them."""
		if not self.machine:
			frappe.throw("Set a Machine before installing exporters.")
		frappe.enqueue_doc(
			self.doctype, self.name, "provision_exporters", queue="long", timeout=1800
		)
		frappe.msgprint(f"Installing the metrics exporters on {self.name} — watch its Ansible Plays.")

	def provision_exporters(self):
		return run_exporters_play(self)

	@frappe.whitelist()
	def deploy_agent(self):
		"""Button: build the latest gateway binary and deploy just it (copy +
		service restart) to this already-provisioned proxy."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_deploy_agent",
			queue="long",
			timeout=1200,
		)
		frappe.msgprint(f"Building + deploying latest agent to {self.name} — watch its Ansible Plays.")

	def _deploy_agent(self):
		"""Push gateway_service source to Proxy Server, build + restart (no
		OpenResty/Redis reinstall)."""
		return self.run_playbook(
			"deploy_agent.yml", extravars={"agent_source": gateway_service_source()}
		)

	@frappe.whitelist()
	def setup(self):
		"""Provision this proxy (OpenResty + Redis + Go agent) via proxy.yml."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"provision",
			queue="long",
			timeout=1800,
		)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.")

	def provision(self):
		"""Run proxy.yml against the Proxy Server's Machine → OpenResty + Redis +
		Go agent. On success, mark Active and project keys/routes."""
		frappe.db.set_value("Proxy Server", self.name, "status", "Installing")
		frappe.db.commit()

		settings = frappe.get_single("Grove Settings")
		play_name, rc = self.run_playbook(
			"proxy.yml",
			extravars={
				"admin_token": self.get_password("admin_token"),
				"agent_source": gateway_service_source(),
				"gateway_id": self.name,
				"proxy_hostname": self.hostname,
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
			"Proxy Server",
			self.name,
			{"status": "Active" if rc == 0 else "Broken", "admin_url": self.admin_url},
		)
		frappe.db.commit()

		if rc == 0:
			# DNS before the sync: the routes push goes to admin_url, which is this box's own
			# name the moment a zone is set, and nothing resolves it until this runs.
			self.sync_dns_records()
			# provision writes status and public_ip through db.set_value, so on_update never
			# fires here — this is the only thing that lets a new proxy reach an engine.
			sync_fleet_ingress()
			gateway_sync.full_sync(proxies=[self.name], trigger="Provision")
		return play_name, rc
