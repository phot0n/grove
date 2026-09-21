# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import re

import bcrypt
import frappe
from frappe.model.document import Document
from frappe.utils import get_url


# The SD token rides in a query string, so it is restricted to URL-unreserved characters: a `&`
# or `#` in it would truncate the value the endpoint compares against.
SD_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,}$")



def verify_scrape_password(password: str, stored: str) -> bool:
	"""False for anything unreadable — a hash from the older sha256-crypt scheme lands here and is
	simply replaced."""
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
		dns_provider: DF.Link | None
		metrics_remote_write_url: DF.Data | None
		monitoring_extra_labels: DF.SmallText | None
		pathway_release: DF.Data | None
		pathway_repo: DF.Data | None
		pod_geography: DF.Link | None
		scrape_password: DF.Password | None
		scrape_password_hash: DF.Data | None
		sd_token: DF.Password | None
		synthetic_session_ttl: DF.Data | None
		weights_bucket: DF.Data | None
		weights_s3_access_key_id: DF.Data | None
		weights_s3_region: DF.Data | None
		weights_s3_secret_access_key: DF.Password | None
		weights_s3_write_access_key_id: DF.Data | None
		weights_s3_write_secret_access_key: DF.Password | None
	# end: auto-generated types

	def validate(self):
		url = self.metrics_remote_write_url or ""
		if url and not url.startswith(("http://", "https://")):
			frappe.throw("Metrics Remote Write URL must start with http:// or https://.")
		if url.startswith("http://"):
			# Warned, not refused: the ingestion service is plain HTTP today, and blocking it here
			# would only get the URL faked.
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
		"""The only guard on an endpoint that returns the fleet's inventory.

		ponytail: parked — the check below is commented out."""
		token = self.get_password("sd_token", raise_exception=False) or ""
		if not token:
			return
		# if not SD_TOKEN_PATTERN.match(token):
		# 	frappe.throw(
		# 		"Service Discovery Token must be at least 16 characters of letters, digits, "
		# 		"'-', '_', '.' or '~'. Generate one with: openssl rand -hex 32"
		# 	)

	def set_scrape_password_hash(self):
		"""The hash each box writes into its htpasswd file, derived here so the password never
		reaches a box or an Ansible argv.

		bcrypt because three things verify this one file — the gateway's Go process, the nginx
		fronting every inference box, and anything else reading an htpasswd — and it is the only
		format all three accept.

		Recomputed only when the password CHANGES: bcrypt salts randomly, so hashing every save
		would rewrite the file on every box and reload nginx for nothing.

		Refused past 72 bytes rather than truncated, which is what bcrypt does silently and would
		make two different long passwords interchangeable."""
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
		"""The fleet-wide half of a Monitoring Agent's vars. What identifies ONE agent — its token,
		its region's endpoint, its intervals — comes from its own doc."""
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
	def weights_s3_engine_environment(self):
		"""Env for an engine that touches the weights bucket: read-only keys plus the streamer
		tuning AWS benchmarks best (4 GB chunks). Empty when the bucket isn't configured."""
		if not (self.weights_bucket and self.weights_s3_access_key_id):
			return {}
		return {
			"AWS_ACCESS_KEY_ID": self.weights_s3_access_key_id,
			"AWS_SECRET_ACCESS_KEY": self.get_password("weights_s3_secret_access_key", raise_exception=False) or "",
			"AWS_DEFAULT_REGION": self.weights_s3_region or "",
			"RUNAI_STREAMER_CHUNK_BYTESIZE": "4294967296",
			"RUNAI_STREAMER_S3_REQUEST_TIMEOUT_MS": "3000",
			"RUNAI_STREAMER_S3_LOW_SPEED_LIMIT": "1048576",
		}

	@property
	def weights_s3_write_environment(self):
		"""Env for the mirror job only — the pair that may write under models/*."""
		if not (self.weights_bucket and self.weights_s3_write_access_key_id):
			return {}
		return {
			"AWS_ACCESS_KEY_ID": self.weights_s3_write_access_key_id,
			"AWS_SECRET_ACCESS_KEY": self.get_password("weights_s3_write_secret_access_key", raise_exception=False) or "",
			"AWS_DEFAULT_REGION": self.weights_s3_region or "",
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
