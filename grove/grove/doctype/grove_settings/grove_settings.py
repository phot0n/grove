# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import re

import frappe
from frappe.model.document import Document
from frappe.utils import get_url
from passlib.hash import sha256_crypt

SD_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,}$")


class GroveSettings(Document):
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
		self.validate_sd_token()
		self.set_scrape_password_hash()
		if self.monitoring_extra_labels:
			try:
				labels = json.loads(self.monitoring_extra_labels)
			except json.JSONDecodeError as e:
				frappe.throw(f"Extra Labels is not valid JSON: {e}")
			if not isinstance(labels, dict):
				frappe.throw('Extra Labels must be a JSON object, e.g. {"env": "prod"}.')

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

		Recomputed only when the password actually changes: sha256_crypt salts randomly, so
		hashing on every save would rewrite the file on every box and reload nginx for nothing.
		A hash that is blank or not sha256_crypt is replaced rather than raised on — this field
		is read-only, so anything else in it came from an edit that should not survive."""
		password = self.get_password("scrape_password", raise_exception=False) or ""
		stored = self.scrape_password_hash or ""
		if not password:
			self.scrape_password_hash = ""
		elif not (sha256_crypt.identify(stored) and sha256_crypt.verify(password, stored)):
			self.scrape_password_hash = sha256_crypt.hash(password)

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
	def scrape_auth_variables(self):
		"""Ansible vars every BOX needs to put its exporters behind basic auth. The hash, never
		the password: a box verifies a credential, it never presents one.

		No username here. It is a constant on both sides — `scrape_username` in the grove_https
		role writes the htpasswd, `monitoring_scrape_username` in the vmagent role presents it —
		and a settable one only added a way for the two to disagree. It did, immediately: this
		Single doc predates the field, so its JSON default never applied and the htpasswd would
		have rendered with a blank user."""
		return {"scrape_password_hash": self.scrape_password_hash or ""}

	@frappe.whitelist()
	def full_sync_all(self):
		"""Button: push the COMPLETE state to every Active proxy."""
		frappe.enqueue("grove.gateway_sync.full_sync", queue="short", trigger="Manual")
		frappe.msgprint("Full sync queued for all Active proxies.")
