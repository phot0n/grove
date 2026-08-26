# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""A box the fleet names.

Gateway Server and Ingress Server both stand on a Machine, answer to `<name>.<fleet zone>` under
the fleet wildcard, run the same Go agent, and are reached by the control plane at their own admin
URL. What they share lives here. What differs — which playbook provisions them, which DNS records
they own, what the agent is given — stays on the doctype.

Deliberately a mixin over two doctypes rather than one doctype with a role flag: an ingress holds
no tenant state, and a doctype with no tenant fields cannot be talked into being pushed keys."""

import frappe

from grove.ansible import AnsibleHost
from grove.cloud_provider.dns import Route53Client
from grove.monitoring import run_exporters_play
from grove.tls import dns_credentials
from grove.utils import validate_id_safe_name

# What a gateway's DNS needs from Grove Settings: the zone every box is named under, the shared name
# the latency tier sits at, and the credential. Read by the Gateway Server for its own row and by the
# Region that owns the tier above it.
GATEWAY_DNS_SETTINGS = ("fleet_zone", "gateway_host", "dns_provider")

def gateway_latency_routing_enabled():
	"""Whether the shared name is a per-region latency tier or one record set on its own.

	Off, every gateway's row sits directly in a multivalue set AT Gateway Host — one record per box,
	exactly as inside a region, minus the CNAME hop and the AWS region name a latency row has to be
	measured against. That is the development answer: a fleet in one place has no region to be nearest
	to, and a Region that is not an AWS one has no latency reference to give."""
	return bool(frappe.db.get_single_value("Grove Settings", "gateway_latency_routing"))


def gateway_health_checks_enabled():
	"""Whether Grove writes Route53 health checks for the fleet.

	Off is the development answer: every row then resolves unconditionally, because Route53 counts a
	record with no check as healthy — so a one-box fleet behind a real zone still works, and nothing
	is billed per check. The tiers themselves are unaffected; only ejection is."""
	return bool(frappe.db.get_single_value("Grove Settings", "gateway_health_checks"))


def gateway_agent_version():
	"""The pathway release every box in the fleet runs.

	A setting rather than a constant: the agent lives in its own repo, so which binary a fleet is on
	is operational state, and pinning or rolling one back is an edit and a Deploy Agent instead of a
	control-plane deploy. Read per run, so it takes effect on the next one.

	Refused blank rather than defaulted. A Single doc that predates a field never applies its JSON
	default, so blank is a state a real site lands in — and it renders an empty release tag into the
	download URL, which 404s a play twenty minutes in instead of saying what is wrong."""
	version = frappe.db.get_single_value("Grove Settings", "gateway_agent_version")
	if not version:
		frappe.throw("Set Gateway Agent Version in Grove Settings — it names the release each box installs.")
	return version


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

	def record_agent_version(self, rc):
		"""Remember which agent release this box actually took, on the runs that installed one.

		One repo made skew impossible; two makes it the thing to watch, and a finished play is the
		only moment that knows the answer. Written with db.set_value, like the statuses around it,
		so recording a version never fires on_update and re-syncs the fleet."""
		if rc == 0:
			frappe.db.set_value(self.doctype, self.name, "agent_version", gateway_agent_version())

	@property
	def has_dns_records(self):
		"""Whether this box ever got far enough to have records worth removing.

		The removal paths ask first. dns_client throws when a record's ingredients are missing,
		which is the right answer to "write my records" and the wrong one to "delete me": a server
		that never reached a public IP has nothing in DNS, and refusing to delete it strands the
		doc — and, through the link, the Machine underneath it."""
		return all(self.get(field) for field in self.dns_fields)

	def dns_client(self):
		settings = frappe.get_single("Grove Settings")
		if not all(settings.get(field) for field in self.dns_settings):
			return None, settings
		missing = [self.meta.get_label(field) for field in self.dns_fields if not self.get(field)]
		if missing:
			frappe.throw(f"{self.doctype} {self.name} needs {' and '.join(missing)} before its DNS records.")
		return Route53Client(*dns_credentials(settings)), settings

	@frappe.whitelist()
	def deploy_tls(self):
		return self.run_playbook(
			"deploy_tls.yml",
			project="Gateway Server",
			extravars=frappe.get_single("Grove Settings").tls_variables,
		)

	@frappe.whitelist()
	def install_exporters(self):
		if not self.machine:
			frappe.throw("Set a Machine before installing exporters.")
		frappe.enqueue_doc(self.doctype, self.name, "provision_exporters", queue="long", timeout=1800)
		frappe.msgprint(f"Installing the metrics exporters on {self.name} — watch its Ansible Plays.", alert=True)

	def provision_exporters(self):
		return run_exporters_play(self)
