# Copyright (c) 2026, Grove and contributors
# See license.txt
"""nvidia-smi parsing and the EC2 instance-type parsers. Pure — no site, box or AWS call."""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from grove.cloud_provider.aws import (
	build_ip_permission,
	machine_status,
	parse_gpus,
	parse_instance_store,
	vram_gb_from_mib,
)
from grove.grove.doctype.machine.machine import _scan_message, parse_nvidia_smi

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

	def test_source_group_rule(self):
		permission = build_ip_permission(
			{"protocol": "tcp", "from_port": 8080, "to_port": 8085, "source_group_id": "sg-proxy"}
		)
		self.assertEqual(permission, {
			"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8085,
			"UserIdGroupPairs": [{"GroupId": "sg-proxy"}],
		})


class TestBaremetalMachine(IntegrationTestCase):
	"""Registering a box by hand must not get harder now that AWS fields exist."""

	def test_saves_with_no_cloud_provider(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-baremetal-01", "public_ip": "10.0.0.1",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertFalse(machine.cloud_provider)
		self.assertFalse(machine.provider_type, "an on-prem box shows no AWS section")
		self.assertIsNone(machine.subnet_group_doc, "no Subnet Group and no Cloud Provider default")

	def test_provision_throws_instead_of_reaching_boto3(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-baremetal-02", "public_ip": "10.0.0.2",
			"instance_type": "g6.12xlarge", "root_volume_gb": 500,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			machine.provision()

	def test_dummy_subnet_group_does_not_wipe_manually_set_region(self):
		region = frappe.get_doc({
			"doctype": "Region", "name": "test-baremetal-region", "label": "test-baremetal-region",
		}).insert(ignore_permissions=True)
		self.addCleanup(region.delete, ignore_permissions=True)

		dummy_group = frappe.get_doc({
			"doctype": "Subnet Group", "name": "test-baremetal-dummy-group", "label": "test-baremetal-dummy-group",
		}).insert(ignore_permissions=True)
		self.addCleanup(dummy_group.delete, ignore_permissions=True)

		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-baremetal-03",
			"region": region.name, "subnet_group": dummy_group.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.region, region.name, "a region-less Subnet Group must not clear it")


class TestSubnetGroupResolution(IntegrationTestCase):
	"""subnet_group_doc's override/fallback, and Subnet Group as the one owner of region."""

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
			"doctype": "Cloud Provider", "name": "test-subnet-group-provider", "provider_type": "aws",
			"api_key": "dummy-secret", "access_key_id": "dummy-key",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.provider.delete, ignore_permissions=True)

		self.default_group = frappe.get_doc({
			"doctype": "Subnet Group", "name": "test-default-group", "label": "test-default-group",
			"cloud_provider": self.provider.name, "region": self.region_a.name,
			"subnet_id": "subnet-default",
			"proxy_security_group_ids": "sg-default-proxy",
			"inference_security_group_ids": "sg-default-inference",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.default_group.delete, ignore_permissions=True)
		self.provider.db_set("default_subnet_group", self.default_group.name)
		self.addCleanup(self.provider.db_set, "default_subnet_group", None)

		self.override_group = frappe.get_doc({
			"doctype": "Subnet Group", "name": "test-override-group", "label": "test-override-group",
			"cloud_provider": self.provider.name, "region": self.region_b.name,
			"subnet_id": "subnet-override",
			"proxy_security_group_ids": "sg-override-proxy",
			"inference_security_group_ids": "sg-override-inference",
		}).insert(ignore_permissions=True)
		self.addCleanup(self.override_group.delete, ignore_permissions=True)

	def test_falls_back_to_cloud_provider_default(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-fallback", "cloud_provider": self.provider.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.subnet_group_doc.name, self.default_group.name)

	def test_machine_subnet_group_overrides_default(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-override", "cloud_provider": self.provider.name,
			"subnet_group": self.override_group.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.subnet_group_doc.name, self.override_group.name)

	def test_region_derived_from_subnet_group(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-region", "cloud_provider": self.provider.name,
			"subnet_group": self.override_group.name, "region": self.region_a.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.region, self.region_b.name, "Subnet Group's region wins over the typed one")

	def test_proxy_role_picks_proxy_security_groups(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-role-proxy", "cloud_provider": self.provider.name,
			"subnet_group": self.override_group.name, "security_group_role": "Proxy Server",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(
			machine.get_security_group_ids(machine.subnet_group_doc), ["sg-override-proxy"]
		)

	def test_inference_role_picks_inference_security_groups(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-role-inference", "cloud_provider": self.provider.name,
			"subnet_group": self.override_group.name, "security_group_role": "Inference Server",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(
			machine.get_security_group_ids(machine.subnet_group_doc), ["sg-override-inference"]
		)

	def test_no_role_set_throws_when_subnet_group_present(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-role-missing", "cloud_provider": self.provider.name,
			"subnet_group": self.override_group.name,
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			machine.get_security_group_ids(machine.subnet_group_doc)

	def test_no_subnet_group_is_empty_list_regardless_of_role(self):
		machine = frappe.get_doc({
			"doctype": "Machine", "machine_name": "test-sg-role-no-group",
		}).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)
		self.assertEqual(machine.get_security_group_ids(None), [])


if __name__ == "__main__":
	unittest.main()
