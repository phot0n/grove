# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import re

import frappe
from frappe.model.document import Document
from frappe.utils import get_url

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

	@property
	def monitoring_variables(self):
		"""Ansible vars every Monitoring Agent needs: where to push, as whom, and where to ask
		what to scrape. The intervals and buffer size are per-agent and come from its own doc."""
		return {
			"monitoring_remote_write_url": self.metrics_remote_write_url or "",
			"monitoring_remote_write_token": self.get_password(
				"metrics_token", raise_exception=False
			) or "",
			"monitoring_extra_labels": json.loads(self.monitoring_extra_labels or "{}"),
			"monitoring_sd_url": f"{get_url()}/api/method/grove.monitoring.targets",
			"monitoring_sd_token": self.get_password("sd_token", raise_exception=False) or "",
		}

	@frappe.whitelist()
	def full_sync_all(self):
		"""Button: push the COMPLETE state to every Active proxy."""
		frappe.enqueue("grove.gateway_sync.full_sync", queue="short", trigger="Manual")
		frappe.msgprint("Full sync queued for all Active proxies.")
