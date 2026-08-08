# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""AWS EC2 provider API client. Launches a GPU instance on one durable root volume, reads
its state and IPs back, and reports what its instance type ships. Pure boto3 — no Frappe
deps; Machine assembles the keys and the launch spec.

Only a root volume is attached: weights are hundreds of GB and must survive a stop, so they
live on durable EBS. The local NVMe an instance type ships is left unmounted (the gpu_host
role refuses ephemeral disks) — it is reported here only so an operator can see the scratch
that is going unused."""

import time

from grove.cloud_provider.base import CloudClient, CloudClientError
from grove.utils import vram_gb_from_mib

# EC2 states that are not a Machine status of their own.
INSTANCE_STATUS = {
	"pending": "Provisioning",
	"running": "Active",
	"stopping": "Draining",
	"stopped": "Offline",
	"shutting-down": "Draining",
	"terminated": "Terminated",
}

# AWS spells x86 its own way. Everywhere else in Grove — Engine Image manifests, monitoring_arch
# in the playbooks, Docker's own platform strings — it is amd64, and these values are compared
# against those. arm64 is spelled the same on both sides.
ARCHITECTURE = {"x86_64": "amd64"}


class AWSError(CloudClientError):
	"""Carries AWS's own error code, so a caller can tell "the instance is gone"
	(InvalidInstanceID.NotFound) from a credential or quota failure."""


def parse_instance_store(instance_type_info):
	"""describe_instance_types → the local NVMe the type ships. Ephemeral: wiped on stop and
	terminate, survives a reboot only, and it is not an EBS volume — no volume id, nothing in
	describe_volumes. A type without local storage has no InstanceStorageInfo key at all."""
	storage = instance_type_info.get("InstanceStorageInfo") or {}
	return {
		"disks": sum(disk.get("Count") or 0 for disk in storage.get("Disks") or []),
		"total_gb": storage.get("TotalSizeInGB") or 0,
	}


def root_volume_id(instance):
	"""This instance's root EBS volume id off its own BlockDeviceMappings, or None — block
	device info can lag right behind a fresh launch, or the root could be instance-store."""
	root_device = instance.get("RootDeviceName")
	for mapping in instance.get("BlockDeviceMappings") or []:
		if mapping.get("DeviceName") == root_device:
			return (mapping.get("Ebs") or {}).get("VolumeId")
	return None


def parse_gpus(instance_type_info):
	"""describe_instance_types → Machine GPU rows, one per card. Seeds the table at
	provision time, when there is no driver on the box yet and so no nvidia-smi to ask.
	Scan GPUs overwrites this with the box's own answer; AWS has no UUID to give."""
	gpus = []
	for entry in (instance_type_info.get("GpuInfo") or {}).get("Gpus") or []:
		memory_mib = (entry.get("MemoryInfo") or {}).get("SizeInMiB") or 0
		for _ in range(entry.get("Count") or 0):
			gpus.append({
				"gpu_index": len(gpus),
				"gpu_model": entry.get("Name") or "",
				"vram_gb": vram_gb_from_mib(memory_mib),
				"gpu_uuid": "",
			})
	return gpus


def normalize_architecture(aws_architecture):
	"""An AWS architecture name → Grove's own. Anything AWS adds passes through unmapped rather
	than becoming a wrong answer; a blank stays blank, which is how an on-prem box reads."""
	return ARCHITECTURE.get(aws_architecture, aws_architecture or "")


def parse_architecture(instance_type_info):
	"""describe_instance_types → the one architecture this type runs. AWS returns a list because
	a few old types booted either 32- or 64-bit; every current one names exactly one."""
	architectures = (instance_type_info.get("ProcessorInfo") or {}).get("SupportedArchitectures") or []
	return normalize_architecture(architectures[0] if architectures else "")


def machine_status(ec2_state):
	"""EC2 instance state → Machine status. An unknown state leaves the box Pending rather
	than crashing a sync — AWS can add states, and a stale status is not worth an exception."""
	return INSTANCE_STATUS.get(ec2_state, "Pending")


def cloud_config(public_keys):
	"""user-data that authorises Grove's own SSH keys for root, so Ansible and Machine's own
	SSH (ssh_user defaults to root) can reach the box whatever the EC2 key pair is.

	cloud-init always writes ssh_authorized_keys to root too, but with its default
	disable_root: true, each key is wrapped in a forced command= that prints a message and
	exits instead of giving a shell — the key is present but unusable. disable_root: false
	is what actually grants root a real login with these keys."""
	keys = "\n".join(f"  - {key}" for key in public_keys.splitlines() if key.strip())
	return f"#cloud-config\ndisable_root: false\nssh_authorized_keys:\n{keys}\n" if keys else ""


def build_ip_permission(rule):
	"""One ingress rule ({protocol, from_port, to_port, cidr|source_group_id}) → a boto3
	IpPermission dict. A rule names exactly one source: a CIDR, or a same-VPC security group."""
	permission = {
		"IpProtocol": rule["protocol"], "FromPort": rule["from_port"], "ToPort": rule["to_port"],
	}
	if rule.get("cidr"):
		permission["IpRanges"] = [{"CidrIp": rule["cidr"]}]
	else:
		permission["UserIdGroupPairs"] = [{"GroupId": rule["source_group_id"]}]
	return permission


def _port_rule(port, **source):
	"""One single-port TCP rule from its source — {"cidr": ...} or {"source_group_id": ...}."""
	return {"protocol": "tcp", "from_port": port, "to_port": port, **source}


class EC2Client(CloudClient):
	"""One AWS account in one region. boto3 is imported on first use so a site without it
	installed can still load the Machine doctype."""

	def __init__(self, access_key_id, secret_access_key, region):
		import boto3

		self.region = region
		self.ec2 = boto3.client(
			"ec2",
			aws_access_key_id=access_key_id,
			aws_secret_access_key=secret_access_key,
			region_name=region,
		)

	def run_instance(
		self,
		name,
		instance_type,
		image_id,
		subnet_id,
		security_group_ids,
		root_volume_gb,
		ssh_public_keys="",
	):
		"""Launch one instance, tagged with the Machine's name. The root volume is the only
		disk attached and holds the engine images and the weights, so it is sized for them.
		Returns the parsed instance — IPs may be absent until it runs, so poll_instance_ready.

		ponytail: no second EBS volume — the gpu_host role's data-mount step always falls back
		to root here. Add a data_volume_gb param + a second BlockDeviceMappings entry if a real
		data volume is ever needed on AWS.
		"""
		body = {
			"ImageId": image_id,
			"InstanceType": instance_type,
			"MinCount": 1,
			"MaxCount": 1,
			"BlockDeviceMappings": [{
				"DeviceName": self.get_image_info(image_id)["root_device_name"],
				"Ebs": {"VolumeSize": int(root_volume_gb), "VolumeType": "gp3",
						"DeleteOnTermination": True},
			}],
			"TagSpecifications": [self._tags("instance", name)],
		}
		if subnet_id:
			body["SubnetId"] = subnet_id
		if security_group_ids:
			body["SecurityGroupIds"] = security_group_ids
		user_data = cloud_config(ssh_public_keys)
		if user_data:
			body["UserData"] = user_data
		instances = self._call(self.ec2.run_instances, **body).get("Instances") or []
		if not instances:
			raise AWSError(f"RunInstances returned no instance for {name}")
		return self._parse_instance(instances[0])

	def get_image_info(self, ami_id):
		"""What launching from this AMI needs to agree with: {root_device_name, cpu_architecture}.

		BlockDeviceMappings only resizes the root volume if it names the AMI's own root device
		(/dev/sda1 on most Ubuntu images, /dev/xvda on some). The architecture is what an arm64
		instance type has to be checked against — an AMI built for the other one launches without
		complaint and then never boots."""
		images = self._call(self.ec2.describe_images, ImageIds=[ami_id]).get("Images") or []
		if not images:
			raise AWSError(f"AMI {ami_id} not found in {self.region}")
		return {
			"root_device_name": images[0].get("RootDeviceName") or "/dev/sda1",
			"cpu_architecture": normalize_architecture(images[0].get("Architecture")),
		}

	def get_instance(self, instance_id):
		"""Fetch one instance → parsed (status, public_ip, private_ip, instance_type, image_id,
		root_volume_gb)."""
		reservations = self._call(
			self.ec2.describe_instances, InstanceIds=[instance_id]
		).get("Reservations") or []
		for reservation in reservations:
			for instance in reservation.get("Instances") or []:
				return self._parse_instance(instance)
		raise AWSError(f"Instance {instance_id} not found")

	def get_instance_type_info(self, instance_type):
		"""This instance type's local storage, GPUs, architecture and whether it is bare metal,
		already in Grove's own shape — one describe_instance_types call carries all four."""
		types = self._call(
			self.ec2.describe_instance_types, InstanceTypes=[instance_type]
		).get("InstanceTypes") or []
		if not types:
			raise AWSError(f"Instance type {instance_type} not available in {self.region}")
		info = types[0]
		return {
			"instance_store": parse_instance_store(info),
			"gpus": parse_gpus(info),
			"is_bare_metal": bool(info.get("BareMetal")),
			"cpu_architecture": parse_architecture(info),
		}

	def poll_instance_ready(self, instance_id, timeout_sec=900, poll_interval_sec=10):
		"""Poll until the instance is reachable: running, addressed, and past both of EC2's own
		status checks.

		Running is not reachable. A bare metal instance reports running with a public IP about a
		minute in and then spends another ten to twenty in firmware POST with nothing listening —
		returning on the state alone hands Ansible a box that refuses the connection. Every
		instance has the same gap; metal only makes it wide enough to always lose."""
		start = time.time()
		while time.time() - start < timeout_sec:
			instance = self.get_instance(instance_id)
			if instance["_ec2_state"] in ("terminated", "shutting-down"):
				raise AWSError(f"Instance {instance_id} went {instance['_ec2_state']} while starting")
			if (
				instance["_ec2_state"] == "running"
				and instance["public_ip"]
				and self.is_instance_reachable(instance_id)
			):
				return instance
			time.sleep(poll_interval_sec)
		raise AWSError(f"Instance {instance_id} was not reachable within {timeout_sec}s")

	def is_instance_reachable(self, instance_id):
		"""Whether EC2 reports both of an instance's status checks passing — the system one for
		the host under it, the instance one for the box itself.

		IncludeAllInstances is load-bearing: without it the response silently omits any instance
		that is not already running, and an empty list would be indistinguishable from an answer."""
		statuses = self._call(
			self.ec2.describe_instance_status,
			InstanceIds=[instance_id],
			IncludeAllInstances=True,
		).get("InstanceStatuses") or []
		if not statuses:
			return False
		return all(
			(statuses[0].get(check) or {}).get("Status") == "ok"
			for check in ("SystemStatus", "InstanceStatus")
		)

	def allocate_static_ip(self, instance_id):
		"""Allocate an Elastic IP and put it on the instance. Returns {public_ip, allocation_id}
		— releasing one needs the allocation id, never the address."""
		address = self._call(
			self.ec2.allocate_address,
			Domain="vpc",
			TagSpecifications=[self._tags("elastic-ip", instance_id)],
		)
		self._call(
			self.ec2.associate_address,
			AllocationId=address["AllocationId"],
			InstanceId=instance_id,
		)
		return {"public_ip": address["PublicIp"], "allocation_id": address["AllocationId"]}

	def release_static_ip(self, allocation_id):
		"""Hand an Elastic IP back. Disassociated first — AWS refuses to release one that is
		still attached, and an address left allocated is billed by the hour."""
		addresses = self._call(
			self.ec2.describe_addresses, AllocationIds=[allocation_id]
		).get("Addresses") or []
		for address in addresses:
			if address.get("AssociationId"):
				self._call(self.ec2.disassociate_address, AssociationId=address["AssociationId"])
		self._call(self.ec2.release_address, AllocationId=allocation_id)
		return True

	def create_network(self, name, cidr_block, subnet_cidr_block, availability_zone=""):
		"""Create a VPC with one public subnet: a route to an Internet Gateway and
		auto-assigned public IPs, so a launched instance is reachable over SSH. Uses the VPC's
		own default route table — Grove doesn't need a private subnet today. No AZ given → the
		region's first available one, returned so the caller can record what it got."""
		availability_zone = availability_zone or self.get_first_availability_zone()
		vpc_id = self._call(
			self.ec2.create_vpc, CidrBlock=cidr_block, TagSpecifications=[self._tags("vpc", name)]
		)["Vpc"]["VpcId"]
		self._call(self.ec2.modify_vpc_attribute, VpcId=vpc_id, EnableDnsHostnames={"Value": True})

		subnet_id = self._call(
			self.ec2.create_subnet, VpcId=vpc_id, CidrBlock=subnet_cidr_block,
			AvailabilityZone=availability_zone, TagSpecifications=[self._tags("subnet", name)],
		)["Subnet"]["SubnetId"]
		self._call(
			self.ec2.modify_subnet_attribute, SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True}
		)

		gateway_id = self._call(
			self.ec2.create_internet_gateway,
			TagSpecifications=[self._tags("internet-gateway", name)],
		)["InternetGateway"]["InternetGatewayId"]
		self._call(self.ec2.attach_internet_gateway, InternetGatewayId=gateway_id, VpcId=vpc_id)

		route_tables = self._call(
			self.ec2.describe_route_tables, Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
		)["RouteTables"]
		route_table_id = route_tables[0]["RouteTableId"]
		self._call(
			self.ec2.create_route, RouteTableId=route_table_id,
			DestinationCidrBlock="0.0.0.0/0", GatewayId=gateway_id,
		)

		return {
			"vpc_id": vpc_id, "subnet_id": subnet_id,
			"internet_gateway_id": gateway_id, "route_table_id": route_table_id,
			"availability_zone": availability_zone,
		}

	def get_first_availability_zone(self):
		"""An available AZ in this region. Any one will do — Grove launches a single public
		subnet, and which letter it lands in is not a choice worth asking an operator to make."""
		zones = self._call(
			self.ec2.describe_availability_zones,
			Filters=[{"Name": "state", "Values": ["available"]}],
		).get("AvailabilityZones") or []
		if not zones:
			raise AWSError(f"No availability zone is available in {self.region}")
		return zones[0]["ZoneName"]

	def create_security_group(self, name, description, vpc_id):
		"""Create a security group in this VPC, tagged so it's identifiable as Grove-managed.
		Returns its id — freshly created, with no ingress rules yet."""
		response = self._call(
			self.ec2.create_security_group,
			GroupName=name,
			Description=description,
			VpcId=vpc_id,
			TagSpecifications=[self._tags("security-group", name)],
		)
		return response["GroupId"]

	def authorize_ingress(self, security_group_id, rules):
		"""Open the given ingress rules on an existing security group."""
		self._call(
			self.ec2.authorize_security_group_ingress,
			GroupId=security_group_id,
			IpPermissions=[build_ip_permission(rule) for rule in rules],
		)

	def revoke_ingress(self, security_group_id, rules):
		"""Close the given ingress rules on an existing security group."""
		self._call(
			self.ec2.revoke_security_group_ingress,
			GroupId=security_group_id,
			IpPermissions=[build_ip_permission(rule) for rule in rules],
		)

	def get_ingress_permissions(self, security_group_id, port):
		"""This group's ingress permissions for exactly this port, as AWS reports them."""
		groups = self._call(
			self.ec2.describe_security_groups, GroupIds=[security_group_id]
		).get("SecurityGroups") or []
		if not groups:
			raise AWSError(f"Security group {security_group_id} does not exist")
		return [
			permission
			for permission in groups[0].get("IpPermissions", [])
			if permission.get("FromPort") == port and permission.get("ToPort") == port
		]

	def sync_ingress(self, security_group_id, port, cidrs):
		"""Make this port's ingress exactly `cidrs`. Returns {opened, closed}.

		Scoped to the one port: every other rule on the group is left alone. A diff that revoked
		everything it did not recognise would take 22 with it and lock Grove out of the box.

		Security-group sources are revoked too, not just CIDRs that fell out of the set — the
		desired state is a list of addresses, so any group-to-group rule on this port is by
		definition not in it."""
		permissions = self.get_ingress_permissions(security_group_id, port)
		current = {
			ip_range["CidrIp"]
			for permission in permissions
			for ip_range in permission.get("IpRanges", [])
		}
		source_groups = [
			pair["GroupId"]
			for permission in permissions
			for pair in permission.get("UserIdGroupPairs", [])
		]

		desired = set(cidrs)
		opened, closed = sorted(desired - current), sorted(current - desired)
		if opened:
			self.authorize_ingress(security_group_id, [_port_rule(port, cidr=c) for c in opened])
		if closed or source_groups:
			self.revoke_ingress(
				security_group_id,
				[_port_rule(port, cidr=c) for c in closed]
				+ [_port_rule(port, source_group_id=g) for g in source_groups],
			)
		return {"opened": opened, "closed": closed + source_groups}

	def resize_root_volume(self, instance_id, size_gb, timeout_sec=600, poll_interval_sec=10):
		"""Grow the instance's root volume, waiting until the new size is visible to the OS.

		EBS reports `optimizing` once the size is already usable — the rebalancing behind it can
		run for hours and must not be waited out, so that state ends the poll the same as
		`completed`. AWS refuses a second modification of the same volume for about six hours
		after one; that arrives as a provider error code rather than a guard here."""
		volume_id = self._root_volume_id(instance_id)
		self._call(self.ec2.modify_volume, VolumeId=volume_id, Size=int(size_gb))
		start = time.time()
		while time.time() - start < timeout_sec:
			modifications = self._call(
				self.ec2.describe_volumes_modifications, VolumeIds=[volume_id]
			).get("VolumesModifications") or []
			# Empty right after modify_volume — the record is eventually consistent, so an
			# absent one means "not started yet", not "done".
			state = modifications[0].get("ModificationState") if modifications else "modifying"
			if state in ("optimizing", "completed"):
				return int(size_gb)
			if state == "failed":
				raise AWSError(
					f"EBS failed to resize {volume_id} to {size_gb} GB "
					f"({modifications[0].get('StatusMessage') or 'no reason given'})"
				)
			time.sleep(poll_interval_sec)
		raise AWSError(f"Volume {volume_id} was still resizing after {timeout_sec}s")

	def _root_volume_id(self, instance_id):
		"""The instance's root EBS volume, off its own BlockDeviceMappings. get_instance returns
		the parsed shape, which does not carry them, so this reads the raw instance."""
		reservations = self._call(
			self.ec2.describe_instances, InstanceIds=[instance_id]
		).get("Reservations") or []
		for reservation in reservations:
			for instance in reservation.get("Instances") or []:
				if volume_id := root_volume_id(instance):
					return volume_id
				raise AWSError(f"Instance {instance_id} has no EBS root volume to resize")
		raise AWSError(f"Instance {instance_id} not found")

	def stop_instance(self, instance_id):
		self._call(self.ec2.stop_instances, InstanceIds=[instance_id])
		return True

	def start_instance(self, instance_id):
		self._call(self.ec2.start_instances, InstanceIds=[instance_id])
		return True

	def terminate_instance(self, instance_id):
		self._call(self.ec2.terminate_instances, InstanceIds=[instance_id])
		return True

	@staticmethod
	def _tags(resource_type, name):
		return {
			"ResourceType": resource_type,
			"Tags": [{"Key": "Name", "Value": name}, {"Key": "ManagedBy", "Value": "Grove"}],
		}

	def _parse_instance(self, instance):
		"""_ec2_state is this client's own — poll_instance_ready reads it to tell a genuine
		terminal failure from "not running yet". status is the public field; it's already
		mapped to Grove's vocabulary via machine_status."""
		state = (instance.get("State") or {}).get("Name")
		return {
			"instance_id": instance.get("InstanceId"),
			"status": machine_status(state),
			"public_ip": instance.get("PublicIpAddress"),
			"private_ip": instance.get("PrivateIpAddress"),
			"instance_type": instance.get("InstanceType"),
			"image_id": instance.get("ImageId"),
			"root_volume_gb": self._root_volume_gb(instance),
			"_ec2_state": state,
		}

	def _root_volume_gb(self, instance):
		"""The root EBS volume's size — not on the instance itself, so a second call against
		the volume id root_volume_id finds in its BlockDeviceMappings."""
		volume_id = root_volume_id(instance)
		if not volume_id:
			return 0
		volumes = self._call(self.ec2.describe_volumes, VolumeIds=[volume_id]).get("Volumes") or []
		return volumes[0].get("Size") or 0 if volumes else 0

	@staticmethod
	def _call(operation, **kwargs):
		"""One EC2 call, with botocore's error turned into ours. The AWS message names the
		exact parameter it rejected, so it is worth surfacing whole."""
		from botocore.exceptions import BotoCoreError, ClientError

		try:
			return operation(**kwargs)
		except ClientError as e:
			raise AWSError(str(e), (e.response.get("Error") or {}).get("Code"))
		except BotoCoreError as e:
			raise AWSError(f"AWS API error: {e}")
