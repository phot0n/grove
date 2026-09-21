# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Route53 records for a named fleet box. Pure boto3 — the doctype assembles the arguments.

Customer traffic resolves through ONE multivalue set:

	api.<zone>    A, multivalue answer, one row per GATEWAY, each with its own health check

One record per IP is what escapes "one health check per record": a row carries a single value and a
single check, and up to eight healthy ones are returned. With every row unhealthy Route53 still answers
with up to eight of them, so the last gateway is never dropped from DNS. Each box also gets a plain A
record at its own name.

An ingress gets only its own name — it is addressed by the gateway's route table, not by DNS, and a
gateway ejects one that stops answering. DNS cannot make that call: it cannot tell a broken ingress
from a model with nowhere to go behind it.

Deliberately NOT a CloudClient: that contract is one account in one REGION, and Route53 is global."""

from grove.cloud_provider.base import CloudClientError

TTL = 60

# The failover knob. 30 x 3 + TTL is ~150s of stale answers before a dead gateway leaves the set —
# Route53's defaults, and the base price. 10 with 2 failures is ~50s at +$1/mo per check. Turning
# these also needs update_health_check on the checks that already exist.
HEALTH_CHECK_INTERVAL = 30
HEALTH_CHECK_FAILURES = 3


class Route53Error(CloudClientError):
	"""Carries AWS's own error code: deleting a record already gone is InvalidChangeBatch, not an
	outage."""


class Route53Client:
	"""One AWS account's public DNS. boto3 on first use, like EC2Client."""

	def __init__(self, access_key_id, secret_access_key):
		import boto3

		self.route53 = boto3.client(
			"route53",
			aws_access_key_id=access_key_id,
			aws_secret_access_key=secret_access_key,
			region_name="us-east-1",
		)
		self._zone_ids = {}

	def get_hosted_zone_id(self, zone):
		"""Memoised per client. Looked up by name rather than configured: a wrong id fails as
		"record not found" long after the save. Private zones are skipped — an account holds
		both."""
		if zone in self._zone_ids:
			return self._zone_ids[zone]
		response = self._call(self.route53.list_hosted_zones_by_name, DNSName=zone, MaxItems="10")
		for hosted_zone in response.get("HostedZones") or []:
			if hosted_zone["Name"].rstrip(".") == zone.rstrip(".") and not hosted_zone["Config"]["PrivateZone"]:
				self._zone_ids[zone] = hosted_zone["Id"].split("/")[-1]
				return self._zone_ids[zone]
		raise Route53Error(f"No public Route53 hosted zone for '{zone}' in this account.")

	# Records — a gateway's own pair, an ingress's single name.

	def upsert_gateway_records(self, zone, hostname, gateway_host, public_ip, identifier, health_check_id):
		"""Both records a gateway needs, in one change batch so a box is never half in DNS."""
		row = gateway_row(gateway_host, public_ip, identifier, health_check_id)
		self._replace_other_policy_row(zone, gateway_host, identifier, row)
		return self._submit(
			zone,
			f"Grove UPSERT {identifier}",
			[
				{"Action": "UPSERT", "ResourceRecordSet": a_record(hostname, public_ip)},
				{"Action": "UPSERT", "ResourceRecordSet": row},
			],
		)

	def delete_gateway_records(self, zone, hostname, gateway_host, public_ip, identifier, health_check_id):
		"""Both records on the way out. A DELETE must repeat the record EXACTLY as created — value,
		TTL, routing policy, health check — which is why this takes the upsert's arguments. A row
		left in a live set is a black hole for whoever resolves to it."""
		return self._submit(
			zone,
			f"Grove DELETE {identifier}",
			[
				{"Action": "DELETE", "ResourceRecordSet": a_record(hostname, public_ip)},
				{
					"Action": "DELETE",
					"ResourceRecordSet": gateway_row(gateway_host, public_ip, identifier, health_check_id),
				},
			],
		)

	def _replace_other_policy_row(self, zone, gateway_host, identifier, row):
		"""This box's row at the shared name under another routing policy — a latency row from before
		the multivalue set — is deleted on its own first, because Route53 will not UPSERT one policy
		into another. Deleted verbatim as listed; the caller's write recreates it a moment later."""
		for record in self.find_record_sets(zone, gateway_host):
			if record.get("SetIdentifier") != identifier or record.get("Type") != "A":
				continue
			if not same_routing_policy(record, row):
				self._submit(zone, f"Grove REPLACE {identifier}", [{"Action": "DELETE", "ResourceRecordSet": record}])

	def upsert_ingress_records(self, zone, hostname, public_ip, identifier):
		"""The one record an ingress needs: the name that reaches this box. UPSERT, so a box that
		came back on a new address is corrected by running it again."""
		return self._submit(
			zone, f"Grove UPSERT {identifier}", [{"Action": "UPSERT", "ResourceRecordSet": a_record(hostname, public_ip)}]
		)

	def delete_ingress_records(self, zone, hostname, public_ip, identifier):
		"""The same record on the way out, repeating what the upsert wrote."""
		return self._submit(
			zone, f"Grove DELETE {identifier}", [{"Action": "DELETE", "ResourceRecordSet": a_record(hostname, public_ip)}]
		)

	def find_record_sets(self, zone, name):
		"""Every record set at exactly this name. The listing starts there and is sorted, so one
		page holds them all."""
		response = self._call(
			self.route53.list_resource_record_sets,
			HostedZoneId=self.get_hosted_zone_id(zone),
			StartRecordName=name,
			MaxItems="100",
		)
		return [
			record
			for record in response.get("ResourceRecordSets") or []
			if record.get("Name", "").rstrip(".") == name.rstrip(".")
		]

	def _submit(self, zone, comment, changes):
		"""One change batch — a whole box's records, so nothing is half written."""
		response = self._call(
			self.route53.change_resource_record_sets,
			HostedZoneId=self.get_hosted_zone_id(zone),
			ChangeBatch={"Comment": comment, "Changes": changes},
		)
		return (response.get("ChangeInfo") or {}).get("Id", "")

	# Health checks — one endpoint check per gateway.

	def create_endpoint_health_check(self, public_ip, hostname, caller_reference):
		"""Answered by pathway itself. HTTP on :80 because the plaintext listener serves /healthz
		outright rather than redirecting, and HTTPS is priced and buys nothing here."""
		config = {
			"IPAddress": public_ip,
			"Port": 80,
			"Type": "HTTP",
			"ResourcePath": "/healthz",
			"RequestInterval": HEALTH_CHECK_INTERVAL,
			"FailureThreshold": HEALTH_CHECK_FAILURES,
		}
		if hostname:
			# So the probe arrives with a Host header the gateway knows as its own name.
			config["FullyQualifiedDomainName"] = hostname
		return self._create_health_check(caller_reference, config)

	def delete_health_check(self, health_check_id):
		"""One already gone is not worth blocking a teardown over. A check a record still names is
		REFUSED, which is why the rows come off first."""
		try:
			self._call(self.route53.delete_health_check, HealthCheckId=health_check_id)
		except Route53Error as e:
			if e.code != "NoSuchHealthCheck":
				raise

	def _create_health_check(self, caller_reference, config):
		"""Create, or recover the one this reference already made: a crash between the create and
		the db_set orphans a check that costs money, and the retry would fail forever on the
		duplicate reference."""
		try:
			response = self._call(
				self.route53.create_health_check, CallerReference=caller_reference, HealthCheckConfig=config
			)
		except Route53Error as e:
			if e.code != "HealthCheckAlreadyExists":
				raise
			return self.find_health_check(caller_reference)
		return response["HealthCheck"]["Id"]

	def find_health_check(self, caller_reference):
		"""Route53 has no lookup by reference, so this is a paginated scan. Only reached when
		recovering from a crash mid-create."""
		for page in self.route53.get_paginator("list_health_checks").paginate():
			for check in page.get("HealthChecks") or []:
				if check.get("CallerReference") == caller_reference:
					return check["Id"]
		return None

	@staticmethod
	def _call(operation, **kwargs):
		"""botocore's error turned into ours. AWS names the exact record it rejected, so the
		message is worth surfacing whole."""
		from botocore.exceptions import BotoCoreError, ClientError

		try:
			return operation(**kwargs)
		except ClientError as e:
			raise Route53Error(str(e), (e.response.get("Error") or {}).get("Code"))
		except BotoCoreError as e:
			raise Route53Error(f"AWS API error: {e}")


def same_routing_policy(record, row):
	"""Route53 will not UPSERT one policy into another, so a row that says no here has to be deleted
	and written again."""
	return bool(record.get("MultiValueAnswer")) == bool(row.get("MultiValueAnswer")) and record.get(
		"Region"
	) == row.get("Region")


def a_record(name, public_ip):
	"""A plain address record — one box's own name, and the base of every row below."""
	return {"Name": name, "Type": "A", "TTL": TTL, "ResourceRecords": [{"Value": public_ip}]}


def gateway_row(gateway_host, public_ip, identifier, health_check_id):
	"""One gateway's row in the shared multivalue set. One value and one check per row is what lets
	Route53 drop this box alone out of the answer."""
	row = {
		**a_record(gateway_host, public_ip),
		"SetIdentifier": identifier,
		"MultiValueAnswer": True,
	}
	if health_check_id:
		# A row with no check counts as healthy.
		row["HealthCheckId"] = health_check_id
	return row
