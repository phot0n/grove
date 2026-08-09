# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Route53 records for a named fleet box. Pure boto3 — the doctype assembles the arguments.

A gateway gets two: the name that reaches it alone, and its row in the latency set behind Gateway
Host. An ingress gets only the first. It is addressed by the gateway's route table, not by DNS, so
there is no shared name in front of a Network and nothing here to health-check: an ingress that has
stopped answering is ejected by the gateway, which is the tier that can tell a broken ingress from
a model with nowhere to go.

Deliberately NOT a CloudClient. That contract is one account in one region and every method on it
is abstract; Route53 is global, and adding methods there would break RunPodClient, which cannot
implement any of them.

Latency routing, not geolocation: a latency record set always answers, picking the region closest
to the resolver, while a geolocation set returns nothing at all to a client whose country is not
covered unless someone remembers a `CountryCode: '*'` default."""

from grove.cloud_provider.base import CloudClientError

TTL = 60


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

	def upsert_ingress_records(self, zone, hostname, public_ip, identifier):
		"""The one record an ingress needs: the name that reaches this box. UPSERT, so a box that
		came back on a new address is corrected by running it again.

		No shared name and no health check. A gateway addresses each ingress by this name out of
		its own route table, and ejects one that has stopped answering — a decision DNS cannot
		make, because it cannot tell a broken ingress from a model with nowhere to go behind it."""
		return self._submit(zone, "UPSERT", identifier, hostname, public_ip)

	def delete_ingress_records(self, zone, hostname, public_ip, identifier):
		"""The same record on the way out, repeating what the upsert wrote — Route53 matches a
		DELETE on the whole record, value and TTL included."""
		return self._submit(zone, "DELETE", identifier, hostname, public_ip)

	def _submit(self, zone, action, identifier, hostname, public_ip, shared=None):
		"""This box's own name, plus its row in a shared set when it has one, in ONE change batch —
		a gateway is never reachable by its own name but absent from the latency set, or the
		reverse."""
		changes = [
			{
				"Action": action,
				"ResourceRecordSet": {
					"Name": hostname,
					"Type": "A",
					"TTL": TTL,
					"ResourceRecords": [{"Value": public_ip}],
				},
			}
		]
		if shared:
			changes.append({"Action": action, "ResourceRecordSet": shared})
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
