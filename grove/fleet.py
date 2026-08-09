# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""A box the fleet names.

Gateway Server and Ingress Server both stand on a Machine, answer to `<name>.<fleet zone>` under
the fleet wildcard, run the same Go agent behind OpenResty, and are reached by the control plane
at their own admin URL. What they share lives here. What differs — which playbook provisions them,
which DNS records they own, what the agent is given — stays on the doctype.

Deliberately a mixin over two doctypes rather than one doctype with a role flag: an ingress holds
no tenant state, and a doctype with no tenant fields cannot be talked into being pushed keys."""

import frappe

from grove.ansible import AnsibleHost
from grove.cloud_provider.route53 import Route53Client
from grove.monitoring import run_exporters_play
from grove.tls import dns_credentials
from grove.utils import validate_id_safe_name


class FleetHost(AnsibleHost):
	"""Everything a named fleet box does the same way: its name, its admin URL, its DNS client,
	its certificate and its exporters."""

	# Grove Settings fields that must be set before this box has DNS records at all, and this
	# doc's own fields those records are built from. Named rather than branched on: a gateway's
	# records need Gateway Host and a Region, an ingress's need neither.
	dns_settings = ("fleet_zone", "dns_provider")
	dns_fields = ("public_ip",)

	def before_insert(self):
		# The doc name is this box's id in every request id it touches and its DNS label both.
		# grove.naming generates it, and a generated name is id-safe by construction — but a
		# Region named with a dot would slug into one that is not, so the check stays.
		validate_id_safe_name(self.doctype, self.name)

	def before_rename(self, old_name, new_name, merge=False):
		validate_id_safe_name(self.doctype, new_name)

	@property
	def hostname(self):
		"""The name that reaches THIS box: <doc name>.<fleet zone>, covered by the fleet's
		wildcard. Blank with no zone set, and then the box has no name at all — it is reached by
		IP over plain HTTP, which is how every proxy worked before TLS.

		Doc names are already DNS-legal labels: validate_id_safe_name allows letters, digits
		and '-' only, on insert and on rename."""
		zone = frappe.db.get_single_value("Grove Settings", "fleet_zone")
		return f"{self.name}.{zone}" if zone else ""

	def set_admin_url(self):
		"""Where the control plane reaches this box's agent. Derived, never typed: it has to name
		ONE box, and the shared names deliberately name several at once. Once it is https on a
		name the fleet certificate covers, `requests` verifies it by default."""
		if self.hostname:
			self.admin_url = f"https://{self.hostname}/grove-admin"
		elif self.public_ip:
			self.admin_url = f"http://{self.public_ip}/grove-admin"

	@property
	def has_dns_records(self):
		"""Whether this box ever got far enough to have records worth removing.

		The removal paths ask first. dns_client throws when a record's ingredients are missing,
		which is the right answer to "write my records" and the wrong one to "delete me": a server
		that never reached a public IP has nothing in DNS, and refusing to delete it strands the
		doc — and, through the link, the Machine underneath it."""
		return all(self.get(field) for field in self.dns_fields)

	def dns_client(self):
		"""(Route53Client, Grove Settings) once everything a record needs is set, (None, settings)
		otherwise. Silent rather than loud: a fleet with no zone configured is the pre-TLS setup,
		and provisioning a box there should not start failing."""
		settings = frappe.get_single("Grove Settings")
		if not all(settings.get(field) for field in self.dns_settings):
			return None, settings
		missing = [self.meta.get_label(field) for field in self.dns_fields if not self.get(field)]
		if missing:
			frappe.throw(f"{self.doctype} {self.name} needs {' and '.join(missing)} before its DNS records.")
		return Route53Client(*dns_credentials(settings)), settings

	@frappe.whitelist()
	def deploy_tls(self):
		"""Write the current fleet certificate on this box and reload OpenResty. Nothing else —
		this is what the daily renewal pushes, and it runs against live boxes.

		The play lives in the Gateway Server tree and is doctype-agnostic: it runs one role and
		one config test, the same on either kind of box."""
		return self.run_playbook(
			"deploy_tls.yml",
			project="Gateway Server",
			extravars=frappe.get_single("Grove Settings").tls_variables,
		)

	@frappe.whitelist()
	def install_exporters(self):
		"""Button: install this box's metrics exporters (long job — it SSHes to the box).
		They only listen; the Monitoring Agent named on this doc is what scrapes them."""
		if not self.machine:
			frappe.throw("Set a Machine before installing exporters.")
		frappe.enqueue_doc(self.doctype, self.name, "provision_exporters", queue="long", timeout=1800)
		frappe.msgprint(f"Installing the metrics exporters on {self.name} — watch its Ansible Plays.")

	def provision_exporters(self):
		return run_exporters_play(self)
