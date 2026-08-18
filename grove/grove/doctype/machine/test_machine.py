# Copyright (c) 2026, Grove and contributors
# See license.txt
"""nvidia-smi parsing and the EC2 instance-type parsers. Pure — no site, box or AWS call."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.cloud_provider.aws import (
	AWSError,
	EC2Client,
	build_ip_permission,
	cloud_config,
	machine_status,
	parse_gpus,
	parse_instance_store,
	root_volume_id,
)
from grove.grove.doctype.machine.machine import (
	Machine,
	_scan_message,
	parse_gpu_memory,
	parse_nvidia_smi,
)
from grove.utils import vram_gb_from_mib

# describe_instance_types for a g6.12xlarge, trimmed to the two keys Grove reads.
G6_12XLARGE = {
	"InstanceType": "g6.12xlarge",
	"GpuInfo": {
		"Gpus": [{"Name": "L4", "Manufacturer": "NVIDIA", "Count": 4,
				  "MemoryInfo": {"SizeInMiB": 23040}}],
		"TotalGpuMemoryInMiB": 92160,
	},
	"InstanceStorageInfo": {"Disks": [{"SizeInGB": 940, "Count": 2, "Type": "ssd"}],
							"TotalSizeInGB": 1880},
}


class TestParseNvidiaSmi(unittest.TestCase):
	def test_multi_gpu_box(self):
		stdout = (
			"0, NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97887, GPU-11111111-2222-3333-4444-555555555555\n"
			"1, NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97887, GPU-66666666-7777-8888-9999-000000000000"
		)
		gpus = parse_nvidia_smi(stdout)
		self.assertEqual(len(gpus), 2)
		self.assertEqual(gpus[0]["gpu_index"], 0)
		self.assertEqual(gpus[0]["gpu_model"], "NVIDIA RTX PRO 6000 Blackwell Workstation Edition")
		self.assertEqual(gpus[0]["vram_gb"], 96)  # 97887 MiB rounds up to the marketed 96 GB
		self.assertTrue(gpus[1]["gpu_uuid"].startswith("GPU-6666"))

	def test_vram_rounds_not_truncates(self):
		# 81559 MiB is an 80 GB A100; truncating would call it 79.
		self.assertEqual(parse_nvidia_smi("0, NVIDIA A100-SXM4-80GB, 81559, GPU-x")[0]["vram_gb"], 80)
		self.assertEqual(parse_nvidia_smi("0, Tesla T4, 15360, GPU-x")[0]["vram_gb"], 15)

	def test_skips_warnings_and_blank_lines(self):
		stdout = "\nWARNING: infoROM is corrupted\n0, Tesla T4, 15360, GPU-x\n\n"
		gpus = parse_nvidia_smi(stdout)
		self.assertEqual(len(gpus), 1)
		self.assertEqual(gpus[0]["gpu_model"], "Tesla T4")

	def test_missing_uuid_column(self):
		gpus = parse_nvidia_smi("0, Tesla T4, 15360")
		self.assertEqual(gpus[0]["gpu_uuid"], "")

	def test_unreadable_memory_does_not_crash(self):
		# nvidia-smi prints [N/A] when a card can't report memory.
		self.assertEqual(parse_nvidia_smi("0, Tesla T4, [N/A], GPU-x")[0]["vram_gb"], 0)

	def test_empty_output(self):
		self.assertEqual(parse_nvidia_smi(""), [])
		self.assertEqual(parse_nvidia_smi(None), [])

	def test_driver_mismatch_yields_no_gpus(self):
		# Real rc=18 output: nvidia-smi writes the error to stdout, not stderr, so the parser
		# must not mistake it for inventory.
		stdout = "Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 580.173"
		self.assertEqual(parse_nvidia_smi(stdout), [])


class TestParseGpuMemory(unittest.TestCase):
	"""The live half of the same CSV — index, name, total, uuid, used, free."""

	def test_used_and_free_come_off_the_last_two_columns(self):
		stdout = (
			"0, Tesla T4, 15360, GPU-a, 14000, 1360\n"
			"1, Tesla T4, 15360, GPU-b, 0, 15360"
		)
		rows = parse_gpu_memory(stdout)
		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0], {
			"gpu_index": 0, "gpu_model": "Tesla T4",
			"total_mib": 15360, "used_mib": 14000, "free_mib": 1360,
		})
		self.assertEqual(rows[1]["free_mib"], 15360)

	def test_a_scan_without_the_memory_columns_is_not_reported_as_idle(self):
		# An older box could still answer the four-column query; 0 used would read as a free GPU.
		self.assertEqual(parse_gpu_memory("0, Tesla T4, 15360, GPU-a"), [])

	def test_unreadable_figures_are_skipped(self):
		self.assertEqual(parse_gpu_memory("0, Tesla T4, 15360, GPU-a, [N/A], [N/A]"), [])

	def test_warnings_and_empty_output(self):
		stdout = "WARNING: infoROM is corrupted\n0, Tesla T4, 15360, GPU-a, 100, 15260\n"
		self.assertEqual(len(parse_gpu_memory(stdout)), 1)
		self.assertEqual(parse_gpu_memory(""), [])
		self.assertEqual(parse_gpu_memory(None), [])


class TestScanMessage(unittest.TestCase):
	def test_driver_mismatch_explains_the_cause(self):
		message = _scan_message({"stdout": "Failed to initialize NVML: Driver/library version mismatch"})
		self.assertIn("Failed to initialize NVML", message)
		self.assertIn("reboot", message)

	def test_falls_back_through_stderr_then_msg(self):
		self.assertEqual(_scan_message({"stderr": "nvidia-smi: not found"}), "nvidia-smi: not found")
		self.assertEqual(_scan_message({"msg": "non-zero return code"}), "non-zero return code")
		self.assertEqual(_scan_message({}), "nothing at all")


class TestParseInstanceStore(unittest.TestCase):
	def test_counts_disks_and_total(self):
		self.assertEqual(parse_instance_store(G6_12XLARGE), {"disks": 2, "total_gb": 1880})

	def test_sums_several_disk_groups(self):
		info = {"InstanceStorageInfo": {
			"Disks": [{"SizeInGB": 3750, "Count": 8}, {"SizeInGB": 940, "Count": 1}],
			"TotalSizeInGB": 30940,
		}}
		self.assertEqual(parse_instance_store(info), {"disks": 9, "total_gb": 30940})

	def test_ebs_only_type_is_zeros_not_a_keyerror(self):
		self.assertEqual(parse_instance_store({"InstanceType": "m7i.large"}),
						 {"disks": 0, "total_gb": 0})


class TestParseGpus(unittest.TestCase):
	def test_expands_count_into_rows(self):
		gpus = parse_gpus(G6_12XLARGE)
		self.assertEqual([gpu["gpu_index"] for gpu in gpus], [0, 1, 2, 3])
		self.assertEqual(gpus[0]["gpu_model"], "L4")
		self.assertEqual(gpus[0]["gpu_uuid"], "", "AWS has no UUID to report")

	def test_indexes_run_across_several_entries(self):
		info = {"GpuInfo": {"Gpus": [
			{"Name": "A", "Count": 2, "MemoryInfo": {"SizeInMiB": 16384}},
			{"Name": "B", "Count": 1, "MemoryInfo": {"SizeInMiB": 16384}},
		]}}
		self.assertEqual([gpu["gpu_index"] for gpu in parse_gpus(info)], [0, 1, 2])

	def test_cpu_only_type_has_no_gpus(self):
		self.assertEqual(parse_gpus({"InstanceType": "m7i.large"}), [])


class TestVramGbFromMib(unittest.TestCase):
	def test_rounds_half_up_not_bankers(self):
		# 23040 MiB is exactly 22.5 GiB. Python's round() is banker's and would say 22.
		self.assertEqual(vram_gb_from_mib(23040), 23)

	def test_agrees_with_the_nvidia_smi_readings(self):
		for mib, gb in ((97887, 96), (81559, 80), (15360, 15)):
			self.assertEqual(vram_gb_from_mib(mib), gb)
			self.assertEqual(parse_nvidia_smi(f"0, card, {mib}, GPU-x")[0]["vram_gb"], gb)


class TestMachineStatus(unittest.TestCase):
	def test_maps_every_ec2_state(self):
		self.assertEqual(machine_status("pending"), "Provisioning")
		self.assertEqual(machine_status("running"), "Active")
		self.assertEqual(machine_status("stopped"), "Offline")
		self.assertEqual(machine_status("terminated"), "Terminated")

	def test_unknown_state_falls_back(self):
		self.assertEqual(machine_status("hibernating"), "Pending")
		self.assertEqual(machine_status(None), "Pending")


class TestBuildIpPermission(unittest.TestCase):
	def test_cidr_rule(self):
		permission = build_ip_permission({"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"})
		self.assertEqual(permission, {
			"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
		})

	def test_a_single_address_rule(self):
		# The shape sync_ingress writes: one proxy's public IP allowed on the engine proxy's port.
		permission = build_ip_permission(
			{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "54.251.169.42/32"}
		)
		self.assertEqual(permission, {
			"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
			"IpRanges": [{"CidrIp": "54.251.169.42/32"}],
		})

	def test_source_group_rule(self):
		# No rule Grove writes uses this branch today, but sync_ingress revokes through it: a
		# group-to-group rule somebody added by hand on the port it owns is not in its desired set.
		permission = build_ip_permission(
			{"protocol": "tcp", "from_port": 443, "to_port": 443, "source_group_id": "sg-proxy"}
		)
		self.assertEqual(permission, {
			"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
			"UserIdGroupPairs": [{"GroupId": "sg-proxy"}],
		})


class TestRootVolumeId(unittest.TestCase):
	def test_matches_root_device_by_name(self):
		instance = {
			"RootDeviceName": "/dev/sda1",
			"BlockDeviceMappings": [
				{"DeviceName": "/dev/sdb", "Ebs": {"VolumeId": "vol-data"}},
				{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-root"}},
			],
		}
		self.assertEqual(root_volume_id(instance), "vol-root")

	def test_no_matching_mapping_is_none(self):
		instance = {"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": []}
		self.assertIsNone(root_volume_id(instance))

	def test_missing_keys_is_none(self):
		self.assertIsNone(root_volume_id({}))


class TestCloudConfig(unittest.TestCase):
	def test_grants_root_a_real_login_not_just_the_key(self):
		config = cloud_config("ssh-ed25519 AAAA one")
		self.assertIn("disable_root: false", config, "key present but root shell still blocked")

	def test_lists_every_key(self):
		config = cloud_config("ssh-ed25519 AAAA one\nssh-ed25519 AAAA two")
		self.assertIn("  - ssh-ed25519 AAAA one", config)
		self.assertIn("  - ssh-ed25519 AAAA two", config)

	def test_no_keys_is_empty(self):
		self.assertEqual(cloud_config(""), "")


class TestResizeRootVolume(unittest.TestCase):
	"""A box's root volume holds the OS, the engine images and every weight, so growing it is
	the alternative to relaunching and re-downloading all of it. EBS reports `optimizing` once
	the new size is already usable, with rebalancing that can run for hours behind it — waiting
	for `completed` would block the job on work the filesystem does not need."""

	def client(self, states):
		"""An EC2Client whose volume modification reports `states` in order, one per poll."""
		reported = iter(states)
		client = EC2Client.__new__(EC2Client)  # no boto3, no credentials
		client.region = "ap-south-1"
		client.calls = []
		client.ec2 = SimpleNamespace(
			describe_instances=lambda **kw: {"Reservations": [{"Instances": [{
				"RootDeviceName": "/dev/sda1",
				"BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-1"}}],
			}]}]},
			modify_volume=lambda **kw: client.calls.append(kw) or {},
			describe_volumes_modifications=lambda **kw: {
				"VolumesModifications": [dict(next(reported), VolumeId="vol-1")]
			},
		)
		return client

	def resize(self, client, size_gb=150):
		return client.resize_root_volume("i-1", size_gb, poll_interval_sec=0)

	def test_optimizing_is_done_enough(self):
		client = self.client([{"ModificationState": "modifying"}, {"ModificationState": "optimizing"}])
		self.assertEqual(self.resize(client), 150)
		self.assertEqual(client.calls, [{"VolumeId": "vol-1", "Size": 150}])

	def test_completed_ends_the_poll_too(self):
		self.assertEqual(self.resize(self.client([{"ModificationState": "completed"}])), 150)

	def test_a_failed_modification_raises_with_the_reason(self):
		client = self.client([{"ModificationState": "failed", "StatusMessage": "quota exceeded"}])
		with self.assertRaises(AWSError) as caught:
			self.resize(client)
		self.assertIn("quota exceeded", str(caught.exception))

	def test_it_gives_up_rather_than_polling_forever(self):
		client = self.client([{"ModificationState": "modifying"}] * 50)
		with self.assertRaises(AWSError):
			client.resize_root_volume("i-1", 150, timeout_sec=0, poll_interval_sec=0)

	def test_an_instance_with_no_ebs_root_is_refused(self):
		client = self.client([{"ModificationState": "completed"}])
		client.ec2.describe_instances = lambda **kw: {"Reservations": [{"Instances": [{
			"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [],
		}]}]}
		with self.assertRaises(AWSError):
			self.resize(client)
		self.assertEqual(client.calls, [], "nothing is modified when there is no root volume")


class TestStaticIp(unittest.TestCase):
	"""An Elastic IP is what makes an address survive a Stop — AWS re-issues a different one on
	every Start otherwise, stranding every route pointing at the old address. It is billed while
	allocated, and AWS refuses to release one that is still attached."""

	def client(self, addresses):
		"""An EC2Client whose describe_addresses reports `addresses`, recording every call."""
		client = EC2Client.__new__(EC2Client)  # no boto3, no credentials
		client.region = "ap-south-1"
		client.calls = []
		record = lambda name: lambda **kw: client.calls.append((name, kw)) or {}
		client.ec2 = SimpleNamespace(
			allocate_address=lambda **kw: client.calls.append(("allocate_address", kw))
			or {"PublicIp": "13.1.1.1", "AllocationId": "eipalloc-1"},
			associate_address=record("associate_address"),
			describe_addresses=lambda **kw: {"Addresses": addresses},
			disassociate_address=record("disassociate_address"),
			release_address=record("release_address"),
		)
		return client

	def test_allocate_puts_the_address_on_the_instance(self):
		client = self.client([])
		self.assertEqual(
			client.allocate_static_ip("i-1"),
			{"public_ip": "13.1.1.1", "allocation_id": "eipalloc-1"},
		)
		self.assertIn(
			("associate_address", {"AllocationId": "eipalloc-1", "InstanceId": "i-1"}), client.calls
		)

	def test_an_attached_address_is_detached_before_release(self):
		client = self.client([{"AllocationId": "eipalloc-1", "AssociationId": "eipassoc-1"}])
		client.release_static_ip("eipalloc-1")
		self.assertEqual(
			[name for name, _ in client.calls], ["disassociate_address", "release_address"]
		)

	def test_a_free_address_is_released_straight_away(self):
		client = self.client([{"AllocationId": "eipalloc-1"}])
		client.release_static_ip("eipalloc-1")
		self.assertEqual([name for name, _ in client.calls], ["release_address"])


class TestLaunchResetsStatusOnFailure(IntegrationTestCase):
	"""A launch() failure must not strand a Machine at Provisioning forever — nothing in the
	UI or provision()'s own guard offers a way out of that state once instance_id is blank
	but status says Provisioning."""

	def test_failure_before_instance_id_resets_to_pending(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-launch-failure",
			"instance_type": "g6.12xlarge", "root_volume_gb": 100,
		}).insert(ignore_permissions=True)
		# launch() commits, which ends the test's transaction — so the row outlives the rollback
		# and the cleanup delete has to be committed too, or the next run hits a duplicate.
		self.addCleanup(frappe.db.commit)
		self.addCleanup(machine.delete, ignore_permissions=True)

		# No cloud_provider set — self.cloud_client throws before instance_id is ever touched,
		# same shape as a bad AMI/credentials/quota failure inside run_instance itself.
		with self.assertRaises(frappe.ValidationError):
			machine.launch()
		self.assertEqual(frappe.db.get_value("Machine", machine.name, "status"), "Pending")


class TestBaremetalMachine(IntegrationTestCase):
	"""Registering a box by hand must not get harder now that AWS fields exist."""

	def test_saves_with_no_cloud_provider(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-baremetal-01", "public_ip": "10.0.0.1",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertFalse(machine.cloud_provider)
		self.assertFalse(machine.provider_type, "an on-prem box shows no AWS section")
		self.assertIsNone(machine.network_doc, "no Network and no Cloud Provider default")

	def test_provision_throws_instead_of_reaching_boto3(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-baremetal-02", "public_ip": "10.0.0.2",
			"instance_type": "g6.12xlarge", "root_volume_gb": 500,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			machine.provision()

	def test_dummy_network_does_not_wipe_manually_set_region(self):
		region = frappe.get_doc({
			"doctype": "Region", "name": "test-baremetal-region", "label": "test-baremetal-region",
		}).insert(ignore_permissions=True)
		self.addCleanup(region.delete, ignore_permissions=True)

		# Region is mandatory on Network now, so a blank one only exists as a row predating that
		# — which is exactly the row this guard is here for.
		dummy_network = frappe.get_doc({
			"doctype": "Network", "name": "test-baremetal-dummy-network",
		}).insert(ignore_permissions=True, ignore_mandatory=True)
		self.addCleanup(dummy_network.delete, ignore_permissions=True)

		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-baremetal-03",
			"region": region.name, "network": dummy_network.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.region, region.name, "a region-less Network must not clear it")


class TestLaunchLockedFields(IntegrationTestCase):
	"""The SSH keys go in via user-data at launch, so editing the key afterwards would only make
	the doc disagree with the box. A static IP is not locked like that — it can be attached to a
	running box at any time, by button."""

	def setUp(self):
		self.key = frappe.get_doc({
			"doctype": "SSH Key", "key_name": "test-launch-locked-key",
			"public_key": "ssh-ed25519 AAAA test@grove",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.key.delete, ignore_permissions=True)

	def machine(self, name, **fields):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": name, **fields,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		return machine

	def test_ssh_key_cannot_change_on_a_launched_box(self):
		machine = self.machine("test-launch-locked", instance_id="i-123", ssh_key=self.key.name)
		machine.ssh_key = ""
		with self.assertRaises(frappe.ValidationError):
			machine.save(ignore_permissions=True)

	def test_ssh_key_is_free_to_change_before_launch(self):
		machine = self.machine("test-launch-unlocked", ssh_key=self.key.name)
		machine.ssh_key = ""
		machine.save(ignore_permissions=True)
		self.assertFalse(frappe.db.get_value("Machine", machine.name, "ssh_key"))

	def test_static_ip_is_not_launch_locked(self):
		machine = self.machine("test-static-ip-unlocked", instance_id="i-456")
		machine.is_static_ip = 1
		machine.save(ignore_permissions=True)
		self.assertTrue(frappe.db.get_value("Machine", machine.name, "is_static_ip"))


class TestSyncDependentServers(IntegrationTestCase):
	"""A Machine going Terminated/Offline/Draining must not leave its Proxy/Inference
	Server still claiming Active — that would be a lie about what's actually running."""

	def setUp(self):
		self.machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sync-dependents",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.machine.delete, ignore_permissions=True)

		self.inference_server = frappe.get_doc({
			"doctype": "Inference Server", "name": "test-sync-dependents-inference",
			"machine": self.machine.name, "status": "Active", "data_path": "/opt/vllm",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.inference_server.delete, ignore_permissions=True)

		self.gateway_server = frappe.get_doc({
			"doctype": "Gateway Server", "name": "test-sync-dependents-proxy",
			"machine": self.machine.name, "status": "Active",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.gateway_server.delete, ignore_permissions=True)

	def test_terminated_machine_breaks_active_servers(self):
		self.machine.status = "Terminated"
		self.machine.sync_dependent_servers()
		self.assertEqual(
			frappe.db.get_value("Inference Server", self.inference_server.name, "status"), "Terminated"
		)
		self.assertEqual(
			frappe.db.get_value("Gateway Server", self.gateway_server.name, "status"), "Terminated"
		)

	def test_stopped_machine_marks_servers_broken_not_terminated(self):
		"""Draining/Offline: the box just stopped, Start can still bring it back — Broken,
		not Terminated."""
		self.machine.status = "Draining"
		self.machine.sync_dependent_servers()
		self.assertEqual(
			frappe.db.get_value("Inference Server", self.inference_server.name, "status"), "Broken"
		)

	def test_active_machine_leaves_servers_alone(self):
		self.machine.status = "Active"
		self.machine.sync_dependent_servers()
		self.assertEqual(
			frappe.db.get_value("Inference Server", self.inference_server.name, "status"), "Active"
		)

	def test_non_active_server_is_not_touched(self):
		"""Pending/Installing/Broken already say "not serving" — only Active can go stale."""
		frappe.db.set_value("Inference Server", self.inference_server.name, "status", "Pending")
		self.machine.status = "Terminated"
		self.machine.sync_dependent_servers()
		self.assertEqual(
			frappe.db.get_value("Inference Server", self.inference_server.name, "status"), "Pending"
		)


class TestNetworkResolution(IntegrationTestCase):
	"""network_doc resolution, Network as the one owner of region, and Machine Image
	resolution (Machine's own, else its Network's — no Cloud Provider fallback for either)."""

	def setUp(self):
		self.region_a = frappe.get_doc({
			"doctype": "Region", "name": "test-region-a", "label": "test-region-a",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.region_a.delete, ignore_permissions=True)

		self.region_b = frappe.get_doc({
			"doctype": "Region", "name": "test-region-b", "label": "test-region-b",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.region_b.delete, ignore_permissions=True)

		self.provider = frappe.get_doc({
			"doctype": "Cloud Provider", "name": "test-network-provider", "provider_type": "aws",
			"api_key": "dummy-secret", "access_key_id": "dummy-key",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.provider.delete, ignore_permissions=True)

		self.network = frappe.get_doc({
			"doctype": "Network", "name": "test-network",
			"cloud_provider": self.provider.name, "region": self.region_b.name,
			"subnet_id": "subnet-x",
			"proxy_security_group_ids": "sg-proxy",
			"inference_security_group_ids": "sg-inference",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.network.delete, ignore_permissions=True)

	def test_network_doc_resolves_the_linked_network(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-link", "cloud_provider": self.provider.name,
			"network": self.network.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.network_doc.name, self.network.name)

	def test_region_derived_from_network(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-region", "cloud_provider": self.provider.name,
			"network": self.network.name, "region": self.region_a.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.region, self.region_b.name, "Network's region wins over the typed one")

	def test_proxy_role_picks_proxy_security_groups(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-role-proxy", "cloud_provider": self.provider.name,
			"network": self.network.name, "machine_type": "Gateway Server",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.get_security_group_ids(machine.network_doc), ["sg-proxy"])

	def test_inference_role_picks_inference_security_groups(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-role-inference", "cloud_provider": self.provider.name,
			"network": self.network.name, "machine_type": "Inference Server",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.get_security_group_ids(machine.network_doc), ["sg-inference"])

	def test_monitoring_agent_role_picks_inference_security_groups(self):
		# It needs 22 and nothing else inbound; the proxy list would open 80/443 for nothing.
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-role-agent", "cloud_provider": self.provider.name,
			"network": self.network.name, "machine_type": "Monitoring Agent",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.get_security_group_ids(machine.network_doc), ["sg-inference"])

	def test_no_role_set_throws_when_network_present(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-role-missing", "cloud_provider": self.provider.name,
			"network": self.network.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			machine.get_security_group_ids(machine.network_doc)

	def test_no_network_is_empty_list_regardless_of_role(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-role-no-network",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.get_security_group_ids(None), [])

	def test_region_code_throws_when_no_region(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-no-region", "cloud_provider": self.provider.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			machine.region_code

	def test_resolved_machine_image_prefers_the_machines_own(self):
		self.network.db_set("machine_image", "ami-network-default")
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-image-own", "cloud_provider": self.provider.name,
			"network": self.network.name, "machine_image": "ami-machine-own",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.resolved_machine_image, "ami-machine-own")

	def test_resolved_machine_image_falls_back_to_network(self):
		self.network.db_set("machine_image", "ami-network-default")
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-image-fallback", "cloud_provider": self.provider.name,
			"network": self.network.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.resolved_machine_image, "ami-network-default")

	def test_resolved_machine_image_throws_when_neither_set(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-net-image-missing", "cloud_provider": self.provider.name,
			"network": self.network.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			machine.resolved_machine_image


if __name__ == "__main__":
	unittest.main()


class TestTerminatingABoxTakesItsServersWithIt(unittest.TestCase):
	"""What a Machine going away does to the servers built on it.

	The write has to go THROUGH the document. It used to be db.set_value, which skips validate and
	on_update — so a Terminated gateway never ran remove_dns_records, and terminating a box left a
	Route53 latency record pointing at an instance that no longer existed. Every client that
	resolved to that region fell into it, and nothing in the fleet would ever have corrected it.
	"""

	def cascade(self, status="Terminated", dependents=None, failing=None):
		"""Runs the cascade against fakes. Returns (saved, reported)."""
		dependents = dependents or {"Gateway Server": ["gw-1"]}
		saved, reported = [], []

		class FakeDoc:
			def __init__(self, doctype, name):
				self.doctype, self.name, self.status = doctype, name, None

			def save(self, ignore_permissions=False):
				if (self.doctype, self.name) == failing:
					raise frappe.ValidationError("that box will not validate")
				saved.append((self.doctype, self.name, self.status, ignore_permissions))

		machine = SimpleNamespace(name="mc-1", status=status)
		machine.mark_dependent = lambda dt, n, s: Machine.mark_dependent(machine, dt, n, s)

		with (
			patch("frappe.get_all", side_effect=lambda dt, **kw: dependents.get(dt, [])),
			patch("frappe.get_doc", side_effect=FakeDoc),
			patch("grove.failure.report", side_effect=lambda dt, n, t, d, **kw: reported.append((dt, n))),
		):
			Machine.sync_dependent_servers(machine)
		return saved, reported

	def test_it_saves_rather_than_writing_the_column(self):
		# save() is the whole fix: it is what runs on_update, which is where remove_dns_records and
		# the security-group reconcile live.
		saved, _ = self.cascade()
		self.assertEqual([("Gateway Server", "gw-1", "Terminated", True)], saved)

	def test_an_ingress_is_not_forgotten(self):
		# It was missing from this list until it bit. An ingress carries a machine, a status and DNS
		# records of its own, so being skipped left it Active and still resolving.
		saved, _ = self.cascade(dependents={"Ingress Server": ["ing-1"]})
		self.assertEqual([("Ingress Server", "ing-1", "Terminated", True)], saved)

	def test_every_dependent_kind_is_covered(self):
		saved, _ = self.cascade(dependents={
			"Gateway Server": ["gw-1"], "Ingress Server": ["ing-1"],
			"Inference Server": ["inf-1"], "Monitoring Agent": ["ma-1"],
		})
		self.assertEqual(
			{"Gateway Server", "Ingress Server", "Inference Server", "Monitoring Agent"},
			{doctype for doctype, _, _, _ in saved},
		)

	def test_a_stopped_box_marks_its_servers_broken_not_terminated(self):
		# Offline/Draining is recoverable — Start can bring the box back, and Terminated would say
		# it is gone for good.
		saved, _ = self.cascade(status="Offline")
		self.assertEqual("Broken", saved[0][2])

	def test_a_running_box_changes_nothing(self):
		saved, _ = self.cascade(status="Active")
		self.assertEqual([], saved)

	def test_one_dependent_that_will_not_save_does_not_take_the_rest_with_it(self):
		"""save() runs validate, so a dependent that fails validation for some unrelated reason
		would otherwise abort the termination halfway — leaving the machine gone and its siblings
		still claiming to be Active."""
		saved, reported = self.cascade(
			dependents={"Gateway Server": ["gw-bad", "gw-good"]},
			failing=("Gateway Server", "gw-bad"),
		)
		self.assertEqual([("Gateway Server", "gw-good", "Terminated", True)], saved)
		self.assertEqual([("Gateway Server", "gw-bad")], reported)
