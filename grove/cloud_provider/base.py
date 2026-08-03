# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The provider-agnostic contract Machine and Subnet Group call through. A concrete client
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
		key_pair_name, root_volume_gb, user_data="",
	):
		"""Launch one instance, tagged with the Machine's name. Returns the parsed instance —
		see get_instance for its shape."""

	@abstractmethod
	def get_instance(self, instance_id):
		"""Fetch one instance → {instance_id, state, public_ip, private_ip, instance_type}."""

	@abstractmethod
	def get_instance_type_info(self, instance_type):
		"""The raw facts a provider reports for an instance type (local storage, GPUs, ...)."""

	@abstractmethod
	def poll_instance_ready(self, instance_id, timeout_sec=300, poll_interval_sec=5):
		"""Poll until the instance is running and reachable."""

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
	def create_security_group(self, name, description, vpc_id):
		"""Create a network security group. Returns its id, with no ingress rules yet."""

	@abstractmethod
	def authorize_ingress(self, security_group_id, rules):
		"""Open the given ingress rules on an existing security group."""


def build_cloud_client(provider_type, access_key_id, secret_access_key, region):
	"""The CloudClient for a Cloud Provider's provider_type. Add a provider by adding one
	entry here — Machine and Subnet Group never change."""
	from grove.cloud_provider.aws import EC2Client

	clients = {"aws": EC2Client}
	cls = clients.get(provider_type)
	if not cls:
		raise CloudClientError(f"No cloud client for provider type '{provider_type}'.")
	return cls(access_key_id, secret_access_key, region)
