# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The provider-agnostic contract Machine and Network call through. A concrete client
(EC2Client, ...) implements every method here; neither doctype ever names a concrete class or
branches on provider_type — build_cloud_client is the one place that dispatch happens."""

from abc import ABC, abstractmethod


class CloudClientError(Exception):
	"""A cloud API call failed. Carries the provider's own error code, so a caller can tell
	"the instance is gone" from a credential or quota failure, without knowing which provider
	it's talking to."""

	def __init__(self, message, code=None):
		super().__init__(message)
		self.code = code


class CloudClient(ABC):
	"""One cloud account in one region."""

	@abstractmethod
	def run_instance(
		self, name, instance_type, image_id, subnet_id, security_group_ids,
		root_volume_gb, ssh_public_keys="",
	):
		"""Launch one instance, tagged with the Machine's name and authorised for the given
		SSH public keys (newline-joined) however this provider injects them. Returns the
		parsed instance — see get_instance for its shape."""

	@abstractmethod
	def get_instance(self, instance_id):
		"""Fetch one instance → {instance_id, status, public_ip, private_ip, instance_type,
		image_id, root_volume_gb}. status is already mapped to Grove's own vocabulary
		(Pending/Provisioning/Active/Draining/Offline/Terminated) — callers never see a
		provider's raw state. The launch facts (instance_type, image_id, root_volume_gb) let
		Sync backfill a Machine that was registered by hand instead of launched through Grove."""

	@abstractmethod
	def get_instance_type_info(self, instance_type):
		"""This instance type's facts, already in Grove's own shape:
		{instance_store: {disks, total_gb}, gpus: [{gpu_index, gpu_model, vram_gb, gpu_uuid}],
		is_bare_metal: bool, cpu_architecture: 'amd64' | 'arm64'}. The architecture is in Docker's
		vocabulary, not the provider's, because that is what an Engine Image is matched against."""

	@abstractmethod
	def get_image_info(self, image_id):
		"""What launching from this machine image has to agree with: {root_device_name,
		cpu_architecture}."""

	@abstractmethod
	def poll_instance_ready(self, instance_id, timeout_sec=900, poll_interval_sec=10):
		"""Poll until the instance is reachable — not merely running. A provider reports an
		instance running well before its OS answers, and on a bare metal box that gap is twenty
		minutes wide, so an implementation must wait for whatever readiness signal it has rather
		than for the state to flip."""

	@abstractmethod
	def resize_root_volume(self, instance_id, size_gb, timeout_sec=600, poll_interval_sec=10):
		"""Grow the instance's root volume to size_gb, returning once the new size is visible to
		the OS. Growing only — no provider shrinks a volume in place. The filesystem on top is
		not touched: that is the box's job, via the grow_root playbook."""

	@abstractmethod
	def stop_instance(self, instance_id):
		"""Stop the instance. Its durable storage survives; ephemeral local storage does not."""

	@abstractmethod
	def start_instance(self, instance_id):
		"""Start a stopped instance."""

	@abstractmethod
	def terminate_instance(self, instance_id):
		"""Destroy the instance and its durable storage."""

	@abstractmethod
	def allocate_static_ip(self, instance_id):
		"""Give the instance an address that survives a stop. Returns {public_ip,
		allocation_id} — the id is what releases it later."""

	@abstractmethod
	def release_static_ip(self, allocation_id):
		"""Hand a static IP back, detaching it first if it is still attached. Providers bill for
		one while it is allocated, so this runs before the instance it belongs to is destroyed."""

	@abstractmethod
	def create_network(self, name, cidr_block, subnet_cidr_block, availability_zone=""):
		"""Create a VPC with one public subnet — its own Internet Gateway and a route to it.
		A blank availability_zone lets the provider pick one. Returns {vpc_id, subnet_id,
		internet_gateway_id, route_table_id, availability_zone}."""

	@abstractmethod
	def create_security_group(self, name, description, vpc_id):
		"""Create a network security group. Returns its id, with no ingress rules yet."""

	@abstractmethod
	def authorize_ingress(self, security_group_id, rules):
		"""Open the given ingress rules on an existing security group."""


def build_cloud_client(provider_type, access_key_id, secret_access_key, region):
	"""The CloudClient for a Cloud Provider's provider_type. Add a provider by adding one
	entry here — Machine and Network never change."""
	from grove.cloud_provider.aws import EC2Client

	clients = {"aws": EC2Client}
	cls = clients.get(provider_type)
	if not cls:
		raise CloudClientError(f"No cloud client for provider type '{provider_type}'.")
	return cls(access_key_id, secret_access_key, region)
