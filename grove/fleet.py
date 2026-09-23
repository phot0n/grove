# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""A box the fleet names.

Gateway Server and Ingress Server both stand on a Machine, answer to `<name>.<fleet zone>`, run the
same Go agent and are reached at their own admin URL. A standalone Inference Server takes the name
and the certificate, not the agent. What they share lives here; what differs — the playbook, the
DNS records, what the agent is given — stays on the doctype.

Deliberately a mixin rather than one doctype with a role flag: a doctype with no tenant fields
cannot be talked into being pushed keys."""

import time

import frappe
import requests
from frappe.utils import escape_html

from grove import failure, pathway_sync
from grove.cloud_provider.dns import Route53Client, Route53Error
from grove.monitoring import run_exporters_play
from grove.server import Server
from grove.tls import dns_credentials

def gateway_agent_version():
	"""The pathway release every box runs. A setting rather than a constant: the agent lives in its
	own repo, so rolling one back is an edit and a Deploy Agent rather than a control-plane deploy.

	Refused blank rather than defaulted — a Single that predates a field never applies its JSON
	default, and an empty release tag 404s a play twenty minutes in."""
	version = frappe.db.get_single_value("Grove Settings", "pathway_release")
	if not version:
		frappe.throw("Set Pathway Release in Grove Settings — it names the release each box installs.")
	return version


def pathway_repo():
	"""The GitHub owner/name every box downloads pathway from. No default, so blank is refused."""
	repo = frappe.db.get_single_value("Grove Settings", "pathway_repo")
	if not repo:
		frappe.throw("Set Pathway Repo in Grove Settings — it names the GitHub repo each box downloads pathway from.")
	return repo


def gateway_agent_release():
	"""Extra-vars naming the release a box installs: the tag and the repo it is published in."""
	return {"agent_version": gateway_agent_version(), "agent_repo": pathway_repo()}


class FleetHost(Server):
	"""Everything a named fleet box does the same way: its name, admin URL, DNS client, certificate
	and exporters."""

	# The doc fields this box's DNS records are built from. Named rather than branched on: a gateway's
	# records also need its Geography's endpoint, an ingress's do not.
	dns_fields = ("public_ip",)

	@property
	def hostname(self):
		"""The name that reaches THIS box: <short name>.<its geography's zone>, covered by that
		geography's wildcard — the doc name itself for a box named with its domain. Blank with no
		zone, and then the box has no name at all — it is reached by IP over plain HTTP, which is how
		every proxy worked before TLS.

		The short name is a DNS-legal label: validate_id_safe_name allows letters, digits and '-'
		only, on insert."""
		zone = self.fleet_zone
		return f"{self.short_name}.{zone}" if zone else ""

	@property
	def tls_variables(self):
		"""The zone and wildcard this box fronts itself with, off its Geography. Carries the key, so
		resolve it inside the job."""
		if not self.geography:
			return {"fleet_zone": "", "fleet_tls_cert": "", "fleet_tls_key": ""}
		return frappe.get_doc("Geography", self.geography).tls_variables

	@property
	def has_fleet_name(self):
		"""Whether this box is published in DNS: its geography has a zone, and the fleet a DNS
		Provider to write it with."""
		return bool(self.fleet_zone and frappe.db.get_single_value("Grove Settings", "dns_provider"))

	def set_admin_url(self):
		"""Where the control plane reaches this box's agent. Derived, never typed: it has to name
		ONE box, and the shared names deliberately name several at once. Once it is https on a
		name the fleet certificate covers, `requests` verifies it by default."""
		if self.hostname:
			self.admin_url = f"https://{self.hostname}/grove-admin"
		elif self.public_ip:
			self.admin_url = f"http://{self.public_ip}/grove-admin"

	@property
	def health_url(self):
		"""pathway's /healthz, on the same host and scheme as its admin API."""
		return (self.admin_url or "").removesuffix("/grove-admin") + "/healthz"

	@frappe.whitelist()
	def ping(self):
		"""Button: GET /healthz from the control plane. A 503 is still reachable; its body says why."""
		if not self.admin_url:
			frappe.throw(f"{self.doctype} {self.name} has no Admin URL yet — nothing to ping.")
		response = requests.get(self.health_url, timeout=5)
		milliseconds = round(response.elapsed.total_seconds() * 1000)
		frappe.msgprint(
			f"{self.health_url} answered {response.status_code} in {milliseconds} ms: "
			f"{escape_html(response.text.strip()[:200])}",
			indicator="green" if response.ok else "orange",
		)
		return response.status_code

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
		if not self.has_fleet_name:
			return None
		missing = [self.meta.get_label(field) for field in self.dns_fields if not self.get(field)]
		if missing:
			frappe.throw(f"{self.doctype} {self.name} needs {' and '.join(missing)} before its DNS records.")
		return Route53Client(*dns_credentials())

	@frappe.whitelist()
	def sync_dns_records(self):
		"""Button + provision step: point this box's name at its address. UPSERT, so a box back on a
		new address is corrected by running it again. One record, no shared name."""
		client = self.dns_client()
		if not client:
			return None
		return client.upsert_ingress_records(
			self.fleet_zone, self.hostname, self.get(self.dns_fields[0]), self.name
		)

	def remove_dns_records(self):
		"""This box's record, on the way out. One already gone is not worth blocking a deletion
		over, and only InvalidChangeBatch is tolerated."""
		if not self.has_dns_records:
			return None
		client = self.dns_client()
		if not client:
			return None
		try:
			return client.delete_ingress_records(
				self.fleet_zone, self.hostname, self.get(self.dns_fields[0]), self.name
			)
		except Route53Error as e:
			if e.code != "InvalidChangeBatch":
				raise
			return None

	@frappe.whitelist()
	def deploy_tls(self):
		return self.run_playbook(
			"deploy_tls.yml",
			project="Gateway Server",
			extravars=self.tls_variables,
		)

	@frappe.whitelist()
	def update_scrape_auth(self):
		"""Button: rewrite this box's metrics htpasswd from the current scrape hash, re-running the
		exporters with it (and DCGM if cards have appeared since Setup). Setup installs all of that
		already; this is the path after a Scrape Password rotation, which no box learns of by itself."""
		if not self.machine:
			frappe.throw("Set a Machine before updating its scrape auth.")
		frappe.enqueue_doc(self.doctype, self.name, "provision_exporters", queue="long", timeout=1800)
		frappe.msgprint(f"Updating scrape auth on {self.name} — watch its Ansible Plays.", alert=True)

	def provision_exporters(self):
		return run_exporters_play(self)


class PathwayHost(FleetHost):
	"""A fleet box that runs pathway — a Gateway or an Ingress Server. Its maintenance is a
	config.json key, owned by its `is_in_maintenance`."""

	@property
	def config_variables(self):
		"""What config.json renders: the fleet's tuning, and whether this box is in maintenance. Every
		play that writes the file passes it, so a deploy never flips maintenance."""
		return {
			**frappe.get_single("Grove Settings").gateway_variables,
			"gateway_maintenance": bool(self.is_in_maintenance),
		}

	@frappe.whitelist()
	def set_maintenance(self, on: int):
		"""Button: refuse new requests while running ones finish, or serve again. The box is asked
		first — a pathway that predates the key refuses the whole file, even at its next start."""
		self.get_in_flight()
		self.db_set("is_in_maintenance", on)
		frappe.enqueue_doc(self.doctype, self.name, "apply_config", queue="short", timeout=600)
		frappe.msgprint(f"{'Starting' if on else 'Ending'} maintenance on {self.name}.", alert=True)

	@failure.reports_failure(mark_broken=False)
	def apply_config(self):
		"""Write config.json and SIGUSR1, then read the box back: a rejected reload keeps the old
		config and says so only in the journal."""
		play_name, rc = self.run_playbook("config.yml", extravars=self.config_variables)
		if rc != 0:
			frappe.throw(f"config.yml failed on {self.name} (Ansible Play {play_name}).")
		want = bool(self.is_in_maintenance)
		for _ in range(10):
			if self.get_in_flight()["maintenance"] == want:
				return play_name, rc
			time.sleep(1)
		frappe.throw(f"{self.name} kept maintenance={not want}: pathway rejected config.json, see its journal.")

	def get_in_flight(self):
		"""The box's own answer: {"maintenance": bool, "in_flight": requests still running}."""
		response = requests.get(
			f"{(self.admin_url or '').rstrip('/')}/in-flight",
			headers={"X-Grove-Admin-Token": self.get_password("admin_token")},
			timeout=pathway_sync.TIMEOUT,
		)
		response.raise_for_status()
		return response.json()
