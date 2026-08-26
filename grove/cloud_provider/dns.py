# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Route53 records for a named fleet box. Pure boto3 — the doctype assembles the arguments.

Customer traffic resolves through TWO tiers, because a latency RRSet is keyed on (name, type,
region) and therefore holds exactly one row per region:

	api.<zone>            CNAME, latency, one row per region, health-checked by that region's
	                      calculated check → <region>.api.<zone>
	<region>.api.<zone>   A, multivalue answer, one row per GATEWAY, each with its own health check

One record per IP is what escapes "one health check per record": a multivalue row carries a single
value and a single check, up to eight healthy ones are returned, and Route53 drops the rest. The
region tier's calculated check takes that region's gateway checks as children at threshold 1, so
latency stops answering with a region whose gateways have all died and falls through to the
next-nearest one instead.

An ingress gets neither tier — only the name that reaches it alone. It is addressed by the gateway's
route table, not by DNS, and a gateway ejects one that has stopped answering: a decision DNS cannot
make, because it cannot tell a broken ingress from a model with nowhere to go behind it.

Deliberately NOT a CloudClient. That contract is one account in one region and every method on it
is abstract; Route53 is global, and adding methods there would break RunPodClient, which cannot
implement any of them.

Latency routing, not geolocation: a latency record set always answers, picking the region closest
to the resolver, while a geolocation set returns nothing at all to a client whose country is not
covered unless someone remembers a `CountryCode: '*'` default."""

from grove.cloud_provider.base import CloudClientError

TTL = 60

# The failover knob. 30 x 3 + TTL is ~150s of stale answers before a dead gateway leaves the set —
# Route53's own defaults, and the base price. 10 with 2 failures is ~50s, at +$1/mo per check for
# the fast-interval feature. Turning these also needs update_health_check on the checks that exist.
HEALTH_CHECK_INTERVAL = 30
HEALTH_CHECK_FAILURES = 3

# A region is up while ANY one of its gateways is up. The calculated check is what makes the latency
# tier honest: without it, latency keeps answering with a region whose gateways have all died.
REGION_HEALTH_THRESHOLD = 1


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
		self._zone_ids = {}

	def get_hosted_zone_id(self, zone):
		"""The public hosted zone for this domain, memoised per client. Looked up by name rather
		than configured: an id is one more setting to get wrong, and a wrong one fails as "record
		not found" long after the save. Private zones are skipped — an account can hold both."""
		if zone in self._zone_ids:
			return self._zone_ids[zone]
		response = self._call(self.route53.list_hosted_zones_by_name, DNSName=zone, MaxItems="10")
		for hosted_zone in response.get("HostedZones") or []:
			if hosted_zone["Name"].rstrip(".") == zone.rstrip(".") and not hosted_zone["Config"]["PrivateZone"]:
				self._zone_ids[zone] = hosted_zone["Id"].split("/")[-1]
				return self._zone_ids[zone]
		raise Route53Error(f"No public Route53 hosted zone for '{zone}' in this account.")

	# Records — a gateway's own pair, a region's latency row, an ingress's single name.

	def upsert_gateway_records(
		self, zone, hostname, gateway_host, set_name, public_ip, identifier, health_check_id
	):
		"""Both records a gateway needs, in one change batch so a box is never half in DNS: its own
		name, and its row in the multivalue set it belongs to. `set_name` is that set — the region's,
		under latency routing, or the shared name itself when there is no region tier.

		Rows this box still holds at the shared name and should not are reconciled first."""
		row = gateway_row(set_name, public_ip, identifier, health_check_id)
		changes = self._stale_shared_rows(zone, gateway_host, identifier, row)
		changes += [
			{"Action": "UPSERT", "ResourceRecordSet": a_record(hostname, public_ip)},
			{"Action": "UPSERT", "ResourceRecordSet": row},
		]
		return self._submit(zone, f"Grove UPSERT {identifier}", changes)

	def delete_gateway_records(self, zone, hostname, set_name, public_ip, identifier, health_check_id):
		"""Both records again, on the way out. A DELETE must repeat the record exactly as it was
		created — value, TTL, routing policy, health check — which is why this takes the same
		arguments the upsert did rather than just the names. A row left behind in a live set is a
		black hole for whatever share of customers resolve to it."""
		return self._submit(
			zone,
			f"Grove DELETE {identifier}",
			[
				{"Action": "DELETE", "ResourceRecordSet": a_record(hostname, public_ip)},
				{
					"Action": "DELETE",
					"ResourceRecordSet": gateway_row(set_name, public_ip, identifier, health_check_id),
				},
			],
		)

	def _stale_shared_rows(self, zone, gateway_host, identifier, row):
		"""Deletions for rows this box holds at the shared name that are not the row it is writing.

		Only A records, and only its own: the region tier's row there is a CNAME, and another box's is
		another box's. Two shapes turn up and they cannot be handled the same way.

		A row that is a DIFFERENT record set — the per-box latency row from before the region tier,
		whose replacement now lives one name down — is returned to go in the SAME batch as the new
		writes, so the shared name is never left without an answer.

		A row that is the SAME record set carrying a different routing policy — what a fleet finds at
		the shared name when it turns latency routing off — is deleted here and now, on its own,
		because Route53 will not UPSERT one routing policy into another. The caller's write recreates
		it a moment later."""
		mine = [
			record
			for record in self.find_record_sets(zone, gateway_host)
			if record.get("SetIdentifier") == identifier and record.get("Type") == "A"
		]
		stale = []
		for record in mine:
			# Deleted verbatim as listed: the old shape cannot be reconstructed, since its TTL and
			# Region are whatever it happened to be written with.
			change = {"Action": "DELETE", "ResourceRecordSet": record}
			if not same_record_set(record, row):
				stale.append(change)
			elif not same_routing_policy(record, row):
				self._submit(zone, f"Grove REPLACE {identifier}", [change])
		return stale

	def upsert_region_record(self, zone, gateway_host, region_label, latency_reference, health_check_id):
		"""This region's row in the latency set at the shared name. Owned by the Region rather than
		by a box, because one row stands for every gateway in it."""
		row = region_row(gateway_host, region_label, latency_reference, health_check_id)
		return self._submit(zone, f"Grove UPSERT {region_label}", [{"Action": "UPSERT", "ResourceRecordSet": row}])

	def delete_region_record(self, zone, gateway_host, region_label, latency_reference, health_check_id):
		"""The last gateway in this region has gone. Repeats what the upsert wrote, same as every
		other DELETE here."""
		row = region_row(gateway_host, region_label, latency_reference, health_check_id)
		return self._submit(zone, f"Grove DELETE {region_label}", [{"Action": "DELETE", "ResourceRecordSet": row}])

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
		page holds them all unless a hundred boxes share the name."""
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
		"""One change batch. Every caller sends a whole box's or a whole region's records at once, so
		nothing is ever half written."""
		response = self._call(
			self.route53.change_resource_record_sets,
			HostedZoneId=self.get_hosted_zone_id(zone),
			ChangeBatch={"Comment": comment, "Changes": changes},
		)
		return (response.get("ChangeInfo") or {}).get("Id", "")

	# Health checks — the endpoint check per gateway, the calculated check per region.

	def create_endpoint_health_check(self, public_ip, hostname, caller_reference):
		"""One gateway's check, answered by pathway itself on /healthz. HTTP on :80 because
		the plaintext listener serves that path outright rather than redirecting it, and HTTPS is a
		priced feature buying nothing on a response body that is the word ok."""
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

	def create_calculated_health_check(self, caller_reference, child_ids):
		"""A region's check: up while any one of its gateways is up."""
		return self._create_health_check(
			caller_reference,
			{
				"Type": "CALCULATED",
				"ChildHealthChecks": sorted(child_ids),
				"HealthThreshold": REGION_HEALTH_THRESHOLD,
			},
		)

	def update_calculated_health_check(self, health_check_id, child_ids):
		"""Point a region's check at the gateways it has now — one arrived, or one left."""
		self._call(
			self.route53.update_health_check,
			HealthCheckId=health_check_id,
			ChildHealthChecks=sorted(child_ids),
			HealthThreshold=REGION_HEALTH_THRESHOLD,
		)

	def delete_health_check(self, health_check_id):
		"""One already gone is not an error worth blocking a teardown over. A check a record set or
		a calculated check still names is REFUSED, which is why the rows come off first."""
		try:
			self._call(self.route53.delete_health_check, HealthCheckId=health_check_id)
		except Route53Error as e:
			if e.code != "NoSuchHealthCheck":
				raise

	def _create_health_check(self, caller_reference, config):
		"""Create, or recover the one this reference already made. A crash between the create and the
		db_set that remembers the id would otherwise orphan a check that costs money and answers to
		nobody — and the retry would fail forever on the duplicate reference."""
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
		"""The check created under this reference, or None. Route53 has no lookup by reference, so
		this is a paginated scan — only reached when recovering from a crash mid-create."""
		for page in self.route53.get_paginator("list_health_checks").paginate():
			for check in page.get("HealthChecks") or []:
				if check.get("CallerReference") == caller_reference:
					return check["Id"]
		return None

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


def group_name(gateway_host, region_label):
	"""The multivalue set for one region, under the shared name. Two labels below the zone, so the
	fleet wildcard does not cover it — which costs nothing, because no TLS terminates here: clients
	only ever send the shared name in SNI."""
	return f"{region_label}.{gateway_host}"


def same_record_set(record, row):
	"""Whether two records are the same record set — which for a policy that takes one is name, type
	and SetIdentifier. Route53 matches an UPSERT and a DELETE on exactly this."""
	return (
		record.get("Name", "").rstrip(".") == row["Name"].rstrip(".")
		and record.get("Type") == row["Type"]
		and record.get("SetIdentifier") == row.get("SetIdentifier")
	)


def same_routing_policy(record, row):
	"""Whether two records of the same set answer under the same policy. Route53 will not UPSERT one
	policy into another, so a row that says no here has to be deleted and written again."""
	return bool(record.get("MultiValueAnswer")) == bool(row.get("MultiValueAnswer")) and record.get(
		"Region"
	) == row.get("Region")


def a_record(name, public_ip):
	"""A plain address record — one box's own name, and the base of every row below."""
	return {"Name": name, "Type": "A", "TTL": TTL, "ResourceRecords": [{"Value": public_ip}]}


def gateway_row(set_name, public_ip, identifier, health_check_id):
	"""One gateway's row in the multivalue set it belongs to — its region's, or the shared name itself
	where there is no region tier. One value and one check per row is what lets Route53 drop this box
	alone out of the answer."""
	row = {
		**a_record(set_name, public_ip),
		"SetIdentifier": identifier,
		"MultiValueAnswer": True,
	}
	if health_check_id:
		# A row with no check counts as healthy, which is the whole point of carrying one.
		row["HealthCheckId"] = health_check_id
	return row


def region_row(gateway_host, region_label, latency_reference, health_check_id):
	"""One region's row in the latency set. A CNAME rather than an alias so it carries the region's
	calculated check outright instead of inferring health from the target — an alias with
	EvaluateTargetHealth is also healthy-if-any, but a child row missing a check makes it fail
	silently open."""
	row = {
		"Name": gateway_host,
		"Type": "CNAME",
		"TTL": TTL,
		"ResourceRecords": [{"Value": group_name(gateway_host, region_label)}],
		# SetIdentifier names this region's row in the shared set; Region is what the resolver's
		# latency is measured against. Both are required, and neither can be changed later without
		# deleting the row.
		"SetIdentifier": region_label,
		"Region": latency_reference,
	}
	if health_check_id:
		row["HealthCheckId"] = health_check_id
	return row
