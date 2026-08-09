# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Route53 records for a named fleet box: the name that reaches this box, and this box's share of
the name that reaches several. Pure boto3 — the doctype assembles the arguments.

Two shapes, because the two shared names answer different questions. Gateway Host is a LATENCY set
over the gateway fleet: every client should land on the nearest gateway, and every gateway can
serve every client. A Network's ingress name is a MULTIVALUE set over one VPC's ingresses: the
boxes behind it are interchangeable and none is nearer than another, so the only job is to hand
out one that is alive — which is what the per-record health check buys and plain multi-A does not.

Deliberately NOT a CloudClient. That contract is one account in one region and every method on it
is abstract; Route53 is global, and adding methods there would break RunPodClient, which cannot
implement any of them.

Latency routing, not geolocation: a latency record set always answers, picking the region closest
to the resolver, while a geolocation set returns nothing at all to a client whose country is not
covered unless someone remembers a `CountryCode: '*'` default."""

from grove.cloud_provider.base import CloudClientError

TTL = 60
# What a Route53 health checker asks an ingress, and how patiently. Two failures at 30s is about a
# minute to drop a dead box out of resolution — fast enough that a stopped ingress is not a
# lingering black hole, slow enough that one missed check does not shrink a two-box Network to one.
HEALTH_PORT = 443
HEALTH_PATH = "/healthz"
HEALTH_INTERVAL = 30
HEALTH_FAILURE_THRESHOLD = 2


class Route53Error(CloudClientError):
	"""Carries AWS's own error code, so a caller can tell "no such record" from a permission
	failure. Deleting a record that is already gone is InvalidChangeBatch, not an outage."""


class Route53Client:
	"""One AWS account's public DNS. boto3 is imported on first use, like EC2Client, so a site
	without it installed can still load the doctypes."""

	def __init__(self, access_key_id, secret_access_key):
		import boto3

		self.route53 = boto3.client(
			"route53",
			aws_access_key_id=access_key_id,
			aws_secret_access_key=secret_access_key,
			region_name="us-east-1",
		)

	def get_hosted_zone_id(self, zone):
		"""The public hosted zone for this domain. Looked up by name rather than configured: an
		id is one more setting to get wrong, and a wrong one fails as "record not found" long
		after the save. Private zones are skipped — an account can hold both for one name."""
		response = self._call(self.route53.list_hosted_zones_by_name, DNSName=zone, MaxItems="10")
		for hosted_zone in response.get("HostedZones") or []:
			if hosted_zone["Name"].rstrip(".") == zone.rstrip(".") and not hosted_zone["Config"]["PrivateZone"]:
				return hosted_zone["Id"].split("/")[-1]
		raise Route53Error(f"No public Route53 hosted zone for '{zone}' in this account.")

	def upsert_gateway_records(self, zone, hostname, gateway_host, public_ip, region, identifier):
		"""Both records a proxy needs, in one change batch so a box is never half in DNS: its own
		name, and its entry in the fleet's latency set. UPSERT, so re-provisioning a box that
		came back on a new address corrects both."""
		return self._change(zone, "UPSERT", hostname, gateway_host, public_ip, region, identifier)

	def delete_gateway_records(self, zone, hostname, gateway_host, public_ip, region, identifier):
		"""Both records again, on the way out. A DELETE must repeat the record exactly as it was
		created — value, TTL, routing policy — which is why this takes the same arguments the
		upsert did rather than just the names. A stale entry left in a latency set is a black
		hole for whatever share of customers resolve to it."""
		return self._change(zone, "DELETE", hostname, gateway_host, public_ip, region, identifier)

	def _change(self, zone, action, hostname, gateway_host, public_ip, region, identifier):
		shared = {
			"Name": gateway_host,
			"Type": "A",
			"TTL": TTL,
			"ResourceRecords": [{"Value": public_ip}],
			# SetIdentifier names this box's row in the shared set; Region is what the resolver's
			# latency is measured against. Both are required for a latency record and neither can
			# be changed later without deleting the row.
			"SetIdentifier": identifier,
			"Region": region,
		}
		return self._submit(zone, action, identifier, hostname, public_ip, shared)

	def upsert_ingress_records(self, zone, hostname, ingress_host, public_ip, identifier, health_check_id=""):
		"""Both records an ingress needs, in one change batch so a box is never half in DNS: its
		own name, which the control plane pushes a replica table to, and its row in the Network's
		shared data name, which is the only ingress address a gateway's route table ever holds.

		Adding or removing an ingress is this call and nothing else — no gateway learns how many
		there are."""
		return self._ingress_change(
			zone, "UPSERT", hostname, ingress_host, public_ip, identifier, health_check_id
		)

	def delete_ingress_records(self, zone, hostname, ingress_host, public_ip, identifier, health_check_id=""):
		"""Both records again, on the way out, repeating exactly what the upsert wrote — Route53
		matches a DELETE on the whole record, health check included. The health check itself is a
		separate resource: delete_health_check, and only after this."""
		return self._ingress_change(
			zone, "DELETE", hostname, ingress_host, public_ip, identifier, health_check_id
		)

	def _ingress_change(self, zone, action, hostname, ingress_host, public_ip, identifier, health_check_id):
		shared = {
			"Name": ingress_host,
			"Type": "A",
			"TTL": TTL,
			"ResourceRecords": [{"Value": public_ip}],
			# One row per ingress under one name, each with its own health check. A multivalue
			# set returns up to eight healthy rows at random and omits a row whose check is
			# failing — which a single record carrying N values cannot do, and why a dead ingress
			# behind a plain multi-A keeps being handed out until somebody notices.
			"SetIdentifier": identifier,
			"MultiValueAnswer": True,
		}
		if health_check_id:
			shared["HealthCheckId"] = health_check_id
		return self._submit(zone, action, identifier, hostname, public_ip, shared)

	def sync_health_check(self, identifier, public_ip, hostname, health_check_id=""):
		"""The health check behind one ingress's row in its Network's shared name → its id.

		Updated when the doc already holds an id and created otherwise, because a box that came
		back on a new address needs the check moved, not a second one billed beside it. The
		CallerReference is derived from the name for the same reason: a create that is retried
		with identical settings returns the existing check rather than a duplicate.

		The name goes in as FullyQualifiedDomainName so the check sends the SNI and Host header a
		gateway does, and the address as IPAddress so it reaches THIS box rather than whichever
		one DNS is currently handing out."""
		if health_check_id:
			self._call(
				self.route53.update_health_check,
				HealthCheckId=health_check_id,
				IPAddress=public_ip,
				FullyQualifiedDomainName=hostname,
			)
			return health_check_id
		response = self._call(
			self.route53.create_health_check,
			CallerReference=f"grove-ingress-{identifier}",
			HealthCheckConfig={
				"IPAddress": public_ip,
				"Port": HEALTH_PORT,
				"Type": "HTTPS",
				"ResourcePath": HEALTH_PATH,
				"FullyQualifiedDomainName": hostname,
				"RequestInterval": HEALTH_INTERVAL,
				"FailureThreshold": HEALTH_FAILURE_THRESHOLD,
			},
		)
		return response["HealthCheck"]["Id"]

	def delete_health_check(self, health_check_id):
		"""After the record that references it — Route53 refuses while one still does. A check
		that is already gone is not an error worth blocking a deletion over."""
		try:
			self._call(self.route53.delete_health_check, HealthCheckId=health_check_id)
		except Route53Error as e:
			if e.code != "NoSuchHealthCheck":
				raise

	def _submit(self, zone, action, identifier, hostname, public_ip, shared):
		"""This box's own name and its row in a shared set, in one change batch — a box is never
		reachable by its own name but absent from the set, or the reverse."""
		changes = [
			{
				"Action": action,
				"ResourceRecordSet": {
					"Name": hostname,
					"Type": "A",
					"TTL": TTL,
					"ResourceRecords": [{"Value": public_ip}],
				},
			},
			{"Action": action, "ResourceRecordSet": shared},
		]
		response = self._call(
			self.route53.change_resource_record_sets,
			HostedZoneId=self.get_hosted_zone_id(zone),
			ChangeBatch={"Comment": f"Grove {action} {identifier}", "Changes": changes},
		)
		return (response.get("ChangeInfo") or {}).get("Id", "")

	@staticmethod
	def _call(operation, **kwargs):
		"""One Route53 call, with botocore's error turned into ours — same shape as EC2Client._call.
		AWS names the exact record it rejected, so the message is worth surfacing whole."""
		from botocore.exceptions import BotoCoreError, ClientError

		try:
			return operation(**kwargs)
		except ClientError as e:
			raise Route53Error(str(e), (e.response.get("Error") or {}).get("Code"))
		except BotoCoreError as e:
			raise Route53Error(f"AWS API error: {e}")
