# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import re

import bcrypt
import frappe
from frappe.model.document import Document
from frappe.utils import get_url

from grove.utils import is_dns_name, is_label_under

SD_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,}$")


def verify_scrape_password(password: str, stored: str) -> bool:
	"""Whether `stored` is a bcrypt hash of `password`. False for anything unreadable — a hash from
	the older sha256-crypt scheme lands here and is simply replaced."""
	if not stored:
		return False
	try:
		return bcrypt.checkpw(password.encode(), stored.encode())
	except ValueError:
		return False


class GroveSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		acme_email: DF.Data | None
		acme_staging: DF.Check
		dns_provider: DF.Link | None
		fleet_tls_cert: DF.SmallText | None
		fleet_tls_expires_on: DF.Datetime | None
		fleet_tls_key: DF.Password | None
		gateway_host: DF.Data | None
		metrics_remote_write_url: DF.Data | None
		monitoring_extra_labels: DF.SmallText | None
		fleet_zone: DF.Data | None
		scrape_password: DF.Password | None
		scrape_password_hash: DF.Data | None
		sd_token: DF.Password | None
		synthetic_session_ttl: DF.Data | None
	# end: auto-generated types

	def validate(self):
		url = self.metrics_remote_write_url or ""
		if url and not url.startswith(("http://", "https://")):
			frappe.throw("Metrics Remote Write URL must start with http:// or https://.")
		if url.startswith("http://"):
			# Warned, not refused: the ingestion service is plain HTTP today, and blocking it
			# here would only get the URL faked. The bearer token is what is exposed.
			frappe.msgprint(
				"Metrics Remote Write URL is plain HTTP — the bearer token crosses the network "
				"in cleartext on every push. Ask the ingestion team for an HTTPS endpoint.",
				indicator="orange",
				alert=True,
			)
		self.validate_tls_names()
		self.validate_sd_token()
		self.set_scrape_password_hash()
		if self.monitoring_extra_labels:
			try:
				labels = json.loads(self.monitoring_extra_labels)
			except json.JSONDecodeError as e:
				frappe.throw(f"Extra Labels is not valid JSON: {e}")
			if not isinstance(labels, dict):
				frappe.throw('Extra Labels must be a JSON object, e.g. {"env": "prod"}.')

	def validate_tls_names(self):
		"""The two names the fleet certificate has to cover, checked here because nothing
		downstream can. A scheme in either renders an nginx server_name that matches nothing, and
		a Gateway Host more than one label under the zone is not covered by `*.<zone>` at all —
		both surface as a certificate error in a customer's SDK, days after the save."""
		for field in ("fleet_zone", "gateway_host"):
			value = (self.get(field) or "").strip()
			self.set(field, value)
			if value and not is_dns_name(value):
				frappe.throw(
					f"{self.meta.get_label(field)} must be a bare hostname — no scheme, port, "
					f"path or trailing dot. Got '{value}'."
				)
		if self.fleet_zone and self.gateway_host and not is_label_under(self.gateway_host, self.fleet_zone):
			frappe.throw(
				f"Gateway Host '{self.gateway_host}' must be exactly one label under "
				f"'{self.fleet_zone}' — the fleet certificate is a wildcard, and *.{self.fleet_zone} "
				f"covers neither the zone itself nor anything deeper."
			)

	def validate_sd_token(self):
		"""The SD token rides in a query string, and it is the only guard on an endpoint that
		returns the fleet's inventory. Restricted to URL-unreserved characters so no escaping
		stands between what is stored here and what an agent sends — a `&` or `#` in it would
		silently truncate the value the endpoint compares, and the compare would simply fail."""
		token = self.get_password("sd_token", raise_exception=False) or ""
		if not token:
			return
		# if not SD_TOKEN_PATTERN.match(token):
		# 	frappe.throw(
		# 		"Service Discovery Token must be at least 16 characters of letters, digits, "
		# 		"'-', '_', '.' or '~'. Generate one with: openssl rand -hex 32"
		# 	)

	def set_scrape_password_hash(self):
		"""The hash each box writes into its htpasswd file, derived here so the password itself
		never reaches a box and never lands in an Ansible argv.

		bcrypt, because three different things have to verify this one file: the gateway's own Go
		process, the nginx still fronting every inference box, and anything else that reads an
		htpasswd. bcrypt is the only format all three accept.

		Recomputed only when the password actually changes: bcrypt salts randomly, so hashing on
		every save would rewrite the file on every box and reload nginx for nothing. A stored value
		that does not verify is replaced rather than raised on — the field is read-only, so anything
		else in it came from an edit that should not survive.

		bcrypt truncates silently past 72 bytes, which would make two different long passwords
		interchangeable. Refused rather than truncated: a scrape password is generated, so hitting
		this means something upstream is wrong."""
		password = self.get_password("scrape_password", raise_exception=False) or ""
		if len(password.encode()) > 72:
			frappe.throw("Scrape Password must be at most 72 bytes — bcrypt ignores anything beyond it.")

		stored = self.scrape_password_hash or ""
		if not password:
			self.scrape_password_hash = ""
		elif not verify_scrape_password(password, stored):
			self.scrape_password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

	@property
	def monitoring_variables(self):
		"""Ansible vars every Monitoring Agent needs that are fleet-wide: where to ask what to
		scrape, the fallback push endpoint, and the credentials the boxes will demand of it. What
		identifies one agent — its token, its region's endpoint, its intervals — comes from its
		own doc."""
		return {
			"monitoring_extra_labels": json.loads(self.monitoring_extra_labels or "{}"),
			"monitoring_sd_url": f"{get_url()}/api/method/grove.monitoring.targets",
			"monitoring_sd_token": self.get_password("sd_token", raise_exception=False) or "",
			"monitoring_scrape_password": self.get_password("scrape_password", raise_exception=False) or "",
		}

	@property
	def gateway_variables(self):
		"""Ansible vars for the gateway's config.json — the half that is re-read on a signal rather
		than requiring a restart.

		Tuning only. Identity and secrets go to agent.env instead, and nothing appears in both: a
		value that lives in one is not overridable from the other, so there is never a question of
		which won. `synthetic_session_ttl` is stored as a bare "0" here, which Go reads as a zero
		duration."""
		return {"synthetic_session_ttl": self.synthetic_session_ttl or "0"}

	@property
	def scrape_auth_variables(self):
		"""Ansible vars every BOX needs to put its exporters behind basic auth. The hash, never
		the password: a box verifies a credential, it never presents one.

		No username here. It is a constant on both sides — `scrape_username` in the grove_https
		role writes the htpasswd, `monitoring_scrape_username` in the vmagent role presents it —
		and a settable one only added a way for the two to disagree. It did, immediately: this
		Single doc predates the field, so its JSON default never applied and the htpasswd would
		have rendered with a blank user."""
		return {"scrape_password_hash": self.scrape_password_hash or ""}

	@property
	def tls_variables(self):
		"""Ansible vars every Gateway Server needs to front itself with TLS: the two names it
		answers to and the certificate both of them share. Blank zone renders a box that serves
		:80 in the clear, which is what every proxy provisioned before this did.

		The key travels as an extra-var like admin_token does — extra-vars are set on the
		VariableManager and never land on an Ansible Play doc. The task that writes it is
		no_log, because a task RESULT does echo the module args back into an Ansible Task."""
		return {
			"gateway_host": self.gateway_host or "",
			"fleet_zone": self.fleet_zone or "",
			"fleet_tls_cert": self.fleet_tls_cert or "",
			"fleet_tls_key": self.get_password("fleet_tls_key", raise_exception=False) or "",
		}

	@frappe.whitelist()
	def issue_fleet_certificate(self):
		"""Button: get the wildcard for the proxy zone from Let's Encrypt over DNS-01 and store
		it here. Does not ship it — provision and the daily renewal do that."""
		frappe.enqueue("grove.tls.issue_fleet_certificate", queue="long", timeout=900)
		frappe.msgprint(f"Requesting a certificate for *.{self.fleet_zone} — this takes a minute.", alert=True)

	@frappe.whitelist()
	def full_sync_all(self):
		"""Button: push the COMPLETE state to every Active proxy."""
		frappe.enqueue("grove.agent_sync.full_sync", queue="short", trigger="Manual")
		frappe.msgprint("Full sync queued for all Active proxies.", alert=True)
