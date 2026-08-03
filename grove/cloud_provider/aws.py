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

# EC2 states that are not a Machine status of their own.
INSTANCE_STATUS = {
	"pending": "Provisioning",
	"running": "Active",
	"stopping": "Draining",
	"stopped": "Offline",
	"shutting-down": "Draining",
	"terminated": "Terminated",
}
MIB_PER_GB = 1024


class AWSError(CloudClientError):
	"""Carries AWS's own error code, so a caller can tell "the instance is gone"
	(InvalidInstanceID.NotFound) from a credential or quota failure."""


def vram_gb_from_mib(mib):
	"""MiB → whole marketed GB. Rounds half up, unlike Python's round(), which is banker's:
	an L4 reporting exactly 23040 MiB is a 24 GB card, and round() would call it 22."""
	return int(mib // MIB_PER_GB + (1 if mib % MIB_PER_GB * 2 >= MIB_PER_GB else 0))


def parse_instance_store(instance_type_info):
	"""describe_instance_types → the local NVMe the type ships. Ephemeral: wiped on stop and
	terminate, survives a reboot only, and it is not an EBS volume — no volume id, nothing in
	describe_volumes. A type without local storage has no InstanceStorageInfo key at all."""
	storage = instance_type_info.get("InstanceStorageInfo") or {}
	return {
		"disks": sum(disk.get("Count") or 0 for disk in storage.get("Disks") or []),
		"total_gb": storage.get("TotalSizeInGB") or 0,
	}


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


def machine_status(ec2_state):
	"""EC2 instance state → Machine status. An unknown state leaves the box Pending rather
	than crashing a sync — AWS can add states, and a stale status is not worth an exception."""
	return INSTANCE_STATUS.get(ec2_state, "Pending")


def cloud_config(public_keys):
	"""user-data that authorises Grove's own SSH keys on the AMI's default user, so Ansible
	can reach the box whatever the EC2 key pair is."""
	keys = "\n".join(f"  - {key}" for key in public_keys.splitlines() if key.strip())
	return f"#cloud-config\nssh_authorized_keys:\n{keys}\n" if keys else ""


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
		key_pair_name,
		root_volume_gb,
		user_data="",
	):
		"""Launch one instance, tagged with the Machine's name. The root volume is the only
		disk attached and holds the engine images and the weights, so it is sized for them.
		Returns the parsed instance — IPs may be absent until it runs, so poll_instance_ready.
		"""
		body = {
			"ImageId": image_id,
			"InstanceType": instance_type,
			"MinCount": 1,
			"MaxCount": 1,
			"BlockDeviceMappings": [{
				"DeviceName": self.get_root_device_name(image_id),
				"Ebs": {"VolumeSize": int(root_volume_gb), "VolumeType": "gp3",
						"DeleteOnTermination": True},
			}],
			"TagSpecifications": [{
				"ResourceType": "instance",
				"Tags": [{"Key": "Name", "Value": name}, {"Key": "ManagedBy", "Value": "Grove"}],
			}],
		}
		if subnet_id:
			body["SubnetId"] = subnet_id
		if security_group_ids:
			body["SecurityGroupIds"] = security_group_ids
		if key_pair_name:
			body["KeyName"] = key_pair_name
		if user_data:
			body["UserData"] = user_data
		instances = self._call(self.ec2.run_instances, **body).get("Instances") or []
		if not instances:
			raise AWSError(f"RunInstances returned no instance for {name}")
		return self._parse_instance(instances[0])

	def get_root_device_name(self, ami_id):
		"""The AMI's own root device (/dev/sda1 on most Ubuntu images, /dev/xvda on some).
		BlockDeviceMappings only resizes the root volume if it names the same device."""
		images = self._call(self.ec2.describe_images, ImageIds=[ami_id]).get("Images") or []
		if not images:
			raise AWSError(f"AMI {ami_id} not found in {self.region}")
		return images[0].get("RootDeviceName") or "/dev/sda1"

	def get_instance(self, instance_id):
		"""Fetch one instance → parsed (state, public_ip, private_ip, instance_type)."""
		reservations = self._call(
			self.ec2.describe_instances, InstanceIds=[instance_id]
		).get("Reservations") or []
		for reservation in reservations:
			for instance in reservation.get("Instances") or []:
				return self._parse_instance(instance)
		raise AWSError(f"Instance {instance_id} not found")

	def get_instance_type_info(self, instance_type):
		"""The raw instance-type description. One call carries both the local-storage and the
		GPU facts, so parse_instance_store and parse_gpus each take what they need from it."""
		types = self._call(
			self.ec2.describe_instance_types, InstanceTypes=[instance_type]
		).get("InstanceTypes") or []
		if not types:
			raise AWSError(f"Instance type {instance_type} not available in {self.region}")
		return types[0]

	def poll_instance_ready(self, instance_id, timeout_sec=300, poll_interval_sec=5):
		"""Poll until the instance is running and has a public IP, so Ansible can connect."""
		start = time.time()
		while time.time() - start < timeout_sec:
			instance = self.get_instance(instance_id)
			if instance["state"] == "running" and instance["public_ip"]:
				return instance
			if instance["state"] in ("terminated", "shutting-down"):
				raise AWSError(f"Instance {instance_id} went {instance['state']} while starting")
			time.sleep(poll_interval_sec)
		raise AWSError(f"Instance {instance_id} was not reachable within {timeout_sec}s")

	def create_security_group(self, name, description, vpc_id):
		"""Create a security group in this VPC, tagged so it's identifiable as Grove-managed.
		Returns its id — freshly created, with no ingress rules yet."""
		response = self._call(
			self.ec2.create_security_group,
			GroupName=name,
			Description=description,
			VpcId=vpc_id,
			TagSpecifications=[{
				"ResourceType": "security-group",
				"Tags": [{"Key": "Name", "Value": name}, {"Key": "ManagedBy", "Value": "Grove"}],
			}],
		)
		return response["GroupId"]

	def authorize_ingress(self, security_group_id, rules):
		"""Open the given ingress rules on an existing security group."""
		self._call(
			self.ec2.authorize_security_group_ingress,
			GroupId=security_group_id,
			IpPermissions=[build_ip_permission(rule) for rule in rules],
		)

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
	def _parse_instance(instance):
		return {
			"instance_id": instance.get("InstanceId"),
			"state": ((instance.get("State") or {}).get("Name")),
			"public_ip": instance.get("PublicIpAddress"),
			"private_ip": instance.get("PrivateIpAddress"),
			"instance_type": instance.get("InstanceType"),
		}

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
