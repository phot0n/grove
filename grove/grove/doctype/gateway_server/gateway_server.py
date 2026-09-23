# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove import pathway_sync
from grove.cloud_provider.dns import Route53Error
from grove.fleet import (
	PathwayHost,
	gateway_agent_release,
	gateway_agent_version,
)
from grove.grove.doctype.gateway_store.gateway_store import store_writers, stores_in
from grove.grove.doctype.network.network import sync_fleet_ingress


class GatewayServer(PathwayHost, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		admin_url: DF.Data | None
		agent_version: DF.Data | None
		frappe_public_key: DF.Code | None
		gateway_store: DF.Link | None
		geography: DF.Link | None
		health_check_id: DF.Data | None
		is_in_maintenance: DF.Check
		is_static_ip: DF.Check
		is_store_writer: DF.Check
		machine: DF.Link
		monitoring_agent: DF.Link | None
		network: DF.Link | None
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	# Records also need the Geography whose endpoint they join.
	dns_fields = ("public_ip", "geography")

	def validate(self):
		self.set_admin_url()
		self.set_admin_token()
		if self.is_store_writer and not self.gateway_store:
			frappe.throw(f"{self.name} is not on a Gateway Store yet — only a gateway deployed onto one can be its writer.")

	def set_admin_token(self):
		"""The credential the control plane authenticates every push with. Generated rather than
		typed: the field is read-only, so there is no way to enter one.

		In validate rather than before_insert, so it also heals docs that exist without one.
		Clearing the field and saving is how it is rotated, which then needs a Deploy Agent —
		the box holds the old value until agent.env is rewritten.

		Tested through get_password, NOT `if not self.admin_token`: a saved Password field reads
		back as asterisks and stays truthy even when the __Auth row holding the real value is
		gone, which is exactly the state this heals."""
		if not self.get_password("admin_token", raise_exception=False):
			self.admin_token = frappe.generate_hash(length=48)

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Terminated":
			self.remove_dns_records()
		# An inference box only answers on 443 to the proxy fleet, and a store on 6379 to its
		# Network's gateways, so a proxy arriving, moving or dying changes what those groups allow.
		if any(self.has_value_changed(field) for field in ("public_ip", "private_ip", "status")):
			sync_fleet_ingress()

	def on_trash(self):
		# While its name still says which records are its own. A row left behind in the multivalue set
		# is a black hole for whichever share of customers resolves to it.
		self.remove_dns_records()
		# Enqueued, so it recomputes after this delete commits and without this proxy.
		sync_fleet_ingress()

	@property
	def archive_blockers(self):
		"""The last gateway of a region is what serves its boxes; with a sibling left, or nothing
		left to serve, it can go."""
		if not self.region or frappe.get_doc("Region", self.region).gateways(exclude=self.name):
			return []
		served = [
			doctype
			for doctype in ("Ingress Server", "Inference Server")
			if frappe.db.exists(doctype, {"region": self.region, "status": ("!=", "Terminated")})
		]
		if not served:
			return []
		return [f"Last gateway in {self.region}, which still has a live {' and '.join(served)}."]

	@property
	def network_store(self):
		"""The Active store of this box's Network, the Network read off the Machine live."""
		network = frappe.db.get_value("Machine", self.machine, "network")
		if not network:
			frappe.throw(f"Machine {self.machine} is in no Network — a gateway runs on its Network's store.")
		stores = stores_in(network, status="Active")
		if not stores:
			frappe.throw(f"Network {network} has no Active Gateway Store — set one up before {self.name}.")
		return stores[0]

	def record_store(self, rc, store):
		"""Which store the agent now runs on, on the runs that wrote agent.env. A writer stays one
		while its store is unchanged, and a store with no Active writer takes this gateway. db.set_value,
		like record_agent_version, so it fires no on_update."""
		if rc != 0:
			return
		before = frappe.db.get_value(self.doctype, self.name, ["gateway_store", "is_store_writer"], as_dict=True)
		stays_writer = before.gateway_store == store and before.is_store_writer
		is_writer = bool(stays_writer or not store_writers(store))
		frappe.db.set_value(
			self.doctype, self.name, {"gateway_store": store, "is_store_writer": int(is_writer)}
		)

	@frappe.whitelist()
	def check_state(self):
		"""Button: which sections a tick would push, pushing nothing."""
		result = pathway_sync.check_state("Gateway Server", self.name)
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
		(logged on a Pathway Sync doc)."""
		frappe.enqueue(
			"grove.pathway_sync.full_sync",
			queue="short",
			proxies=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Full sync queued for {self.name}.", alert=True)

	@property
	def caller_reference(self):
		"""Idempotency token for the health check. The creation stamp is in it so a name reused
		after a terminate cannot collide with the old box's check."""
		# The short name: Route53 caps a caller reference at 64 characters.
		return f"grove-{self.short_name}-{frappe.utils.get_datetime(self.creation):%Y%m%d%H%M%S}"

	def ensure_health_check(self, client):
		"""Created once. It is what drops this box alone out of the multivalue answer when its /healthz
		stops answering 200."""
		if not self.health_check_id:
			self.db_set(
				"health_check_id",
				client.create_endpoint_health_check(self.public_ip, self.hostname, self.caller_reference),
			)
		return self.health_check_id

	@frappe.whitelist()
	def sync_dns_records(self):
		"""Button + provision step: point this box's name at it and put it in the shared multivalue set
		behind its health check. UPSERT, so a box back on a new address is corrected by running it
		again."""
		client = self.dns_client()
		if not client:
			return None
		return client.upsert_gateway_records(
			self.fleet_zone,
			self.hostname,
			frappe.db.get_value("Geography", self.geography, "endpoint"),
			self.public_ip,
			self.name,
			self.ensure_health_check(client),
		)

	def delete_health_check(self, client):
		"""Only ever after its row has let go: Route53 refuses to delete a check a record still names."""
		if self.health_check_id:
			client.delete_health_check(self.health_check_id)
			self.db_set("health_check_id", "")

	def remove_dns_records(self):
		"""Two records, then the health check — in that order, because Route53 refuses to delete a check
		a record still names. A record already gone is not worth blocking a deletion over, and only
		InvalidChangeBatch is tolerated."""
		if not self.has_dns_records:
			return None
		client = self.dns_client()
		if not client:
			return None
		change = None
		try:
			change = client.delete_gateway_records(
				self.fleet_zone,
				self.hostname,
				frappe.db.get_value("Geography", self.geography, "endpoint"),
				self.public_ip,
				self.name,
				self.health_check_id,
			)
		except Route53Error as e:
			if e.code != "InvalidChangeBatch":
				raise
		self.delete_health_check(client)
		return change

	@frappe.whitelist()
	def deploy_agent(self):
		"""Button: install the pinned release on an already-provisioned proxy."""
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
	def _deploy_agent(self, agent_binary="", **play):
		"""Install the pinned agent release — or, for a dev deploy, the build `agent_binary` names
		on the control plane — and rewrite both halves of its configuration. `play` names the doc
		the play is run for, e.g. a Pathway Update.

		One button for binary AND config: agent.env names every listener, certificate and hostname,
		and config.json holds the tunables. Written whole from the same extra-vars provision passes,
		so a config-only run never blanks the admin token.

		Stays on the Redis it is on: moving a live gateway onto a store drains it first, which a deploy
		does not."""
		store = self.gateway_store
		play_name, rc = self.run_playbook(
			"deploy_agent.yml", extravars=self.get_agent_extravars(store, agent_binary), **play
		)
		self.record_agent_version(rc)
		self.record_store(rc, store)
		return play_name, rc

	def get_agent_extravars(self, store, agent_binary=""):
		"""Everything agent.env and config.json render, onto `store`'s Redis. Resolved inside the job,
		so the secrets never sit in a job payload. The fleet key stays out: agent.env names only the
		certificate's path, and deploy_tls owns writing the material."""
		if not store:
			frappe.throw(f"{self.name} is on no Gateway Store — Setup it first.")
		settings = frappe.get_single("Grove Settings")
		tls_variables = self.tls_variables
		tls_variables.pop("fleet_tls_key", None)
		return {
			**gateway_agent_release(),
			"agent_binary": agent_binary,
			"admin_token": self.get_password("admin_token"),
			# Stamped into request ids, which keep only letters, digits and '-'.
			"gateway_id": self.short_name,
			# Which routes this gateway prefers: a same-region row wins outright.
			"gateway_region": self.region or "",
			# Which pinned users it refuses, and the shared name it answers for.
			"gateway_geography": self.geography or "",
			"gateway_host": frappe.db.get_value("Geography", self.geography, "endpoint") if self.geography else "",
			"proxy_hostname": self.hostname,
			**frappe.get_doc("Gateway Store", store).redis_variables,
			**tls_variables,
			**settings.scrape_auth_variables,
			**self.config_variables,
		}

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
	def provision(self, agent_binary=""):
		"""Run gateway.yml → OpenResty + Redis + Go agent. On success, mark Active and project
		keys/routes. `agent_binary`: a dev deploy — a build on the control plane ships instead
		of the pinned release (see roles/install_gateway_agent)."""
		frappe.db.set_value("Gateway Server", self.name, "status", "Installing")
		frappe.db.commit()

		store = self.network_store
		play_name, rc = self.run_playbook(
			"gateway.yml",
			# The fleet key too: Setup is what writes the certificate. Blank zone renders a box that
			# serves :80 in the clear.
			extravars={**self.get_agent_extravars(store, agent_binary), **self.tls_variables},
		)

		# Derived at validate, so a zone set after the last save leaves it naming the old address.
		# Refreshed here so the sync below goes where the box now answers.
		self.set_admin_url()
		frappe.db.set_value(
			"Gateway Server",
			self.name,
			{"status": "Active" if rc == 0 else "Broken", "admin_url": self.admin_url},
		)
		self.record_agent_version(rc)
		self.record_store(rc, store)
		frappe.db.commit()

		if rc == 0:
			# Its name has to resolve before the tick pushes to admin_url, which IS that name once
			# a zone is set.
			self.sync_dns_records()
			# provision writes through db.set_value, so on_update never fires — this is the only
			# thing that lets a new proxy reach an engine.
			sync_fleet_ingress()
		return play_name, rc
