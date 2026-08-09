# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""What a Machine reads off its instance type and its AMI, and when it calls a box ready. Pure —
boto3 is replaced by a fake that answers from canned responses, so no site and no network.

Slow boots are the reason this file exists. A bare metal instance reports `running` with a public
IP about a minute into a boot that takes another ten to twenty, so a poll that trusts the state
hands Ansible a box that refuses the connection, and a reboot on Ansible's default timeout fails
the play midway through provisioning.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from grove.cloud_provider.aws import AWSError, EC2Client, normalize_architecture, parse_architecture

GPU_HOST_ROLE = Path(__file__).parent.parent.parent / "playbooks/inference_server/roles/gpu_host"


class FakeEC2:
	"""Stands in for the boto3 client. Each describe_* answers from a queue, so a test can say
	"initializing, then initializing, then ok" and assert what the poll did in between."""

	def __init__(self, instances=None, statuses=None, types=None, images=None):
		self.instances = list(instances or [])
		self.statuses = list(statuses or [])
		self.types = types or {}
		self.images = images or {}
		self.status_calls = 0

	def _next(self, queue):
		return queue.pop(0) if len(queue) > 1 else queue[0]

	def describe_instances(self, **kwargs):
		return {"Reservations": [{"Instances": [self._next(self.instances)]}]}

	def describe_instance_status(self, **kwargs):
		self.status_calls += 1
		self.last_status_kwargs = kwargs
		return {"InstanceStatuses": self._next(self.statuses)}

	def describe_instance_types(self, **kwargs):
		return {"InstanceTypes": [self.types]} if self.types else {"InstanceTypes": []}

	def describe_images(self, **kwargs):
		return {"Images": [self.images]} if self.images else {"Images": []}

	def describe_volumes(self, **kwargs):
		return {"Volumes": [{"Size": 100}]}


def client(**fake_kwargs):
	"""An EC2Client wired to a FakeEC2, without boto3 or credentials."""
	ec2 = EC2Client.__new__(EC2Client)
	ec2.region = "ap-south-1"
	ec2.ec2 = FakeEC2(**fake_kwargs)
	return ec2


def instance(state="running", public_ip="1.2.3.4"):
	return {
		"InstanceId": "i-1",
		"State": {"Name": state},
		"PublicIpAddress": public_ip,
		"PrivateIpAddress": "10.0.0.5",
		"InstanceType": "g4dn.metal",
		"ImageId": "ami-1",
		"RootDeviceName": "/dev/sda1",
		"BlockDeviceMappings": [],
	}


def status(system="ok", box="ok"):
	return [{"SystemStatus": {"Status": system}, "InstanceStatus": {"Status": box}}]


class TestInstanceTypeFacts(unittest.TestCase):
	def test_bare_metal_and_architecture_come_off_the_type(self):
		info = client(
			types={"BareMetal": True, "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]}}
		).get_instance_type_info("g4dn.metal")
		self.assertTrue(info["is_bare_metal"])
		# amd64, not x86_64: this is compared against an Engine Image's manifest platform.
		self.assertEqual(info["cpu_architecture"], "amd64")

	def test_a_virtualized_arm_type_reads_as_neither_metal_nor_amd64(self):
		info = client(
			types={"ProcessorInfo": {"SupportedArchitectures": ["arm64"]}}
		).get_instance_type_info("g5g.xlarge")
		self.assertFalse(info["is_bare_metal"])
		self.assertEqual(info["cpu_architecture"], "arm64")

	def test_an_unknown_architecture_passes_through_rather_than_becoming_amd64(self):
		self.assertEqual(normalize_architecture("riscv64"), "riscv64")
		self.assertEqual(normalize_architecture(None), "")
		self.assertEqual(parse_architecture({}), "")

	def test_a_type_that_also_boots_32_bit_is_still_read_as_64(self):
		# t2.micro answers ["i386", "x86_64"] to this day. Taking the first entry picked i386,
		# which the Machine's Select rejects — so provisioning the cheapest type in the catalogue
		# failed validation before it ever reached EC2.
		info = {"ProcessorInfo": {"SupportedArchitectures": ["i386", "x86_64"]}}
		self.assertEqual(parse_architecture(info), "amd64")

	def test_the_list_order_is_not_trusted(self):
		for order in (["arm64", "i386"], ["i386", "arm64"]):
			with self.subTest(order):
				self.assertEqual(
					parse_architecture({"ProcessorInfo": {"SupportedArchitectures": order}}), "arm64"
				)

	def test_a_type_grove_cannot_run_still_reports_what_aws_said(self):
		# Better an architecture the Select refuses, naming itself, than a silent wrong answer.
		info = {"ProcessorInfo": {"SupportedArchitectures": ["i386"]}}
		self.assertEqual(parse_architecture(info), "i386")

	def test_a_type_the_region_does_not_have_is_an_error_not_an_empty_answer(self):
		with self.assertRaises(AWSError):
			client(types={}).get_instance_type_info("p5.48xlarge")


class TestImageInfo(unittest.TestCase):
	def test_the_root_device_and_architecture_come_off_the_ami(self):
		info = client(
			images={"RootDeviceName": "/dev/xvda", "Architecture": "arm64"}
		).get_image_info("ami-1")
		self.assertEqual(info, {"root_device_name": "/dev/xvda", "cpu_architecture": "arm64"})

	def test_an_ami_without_a_root_device_falls_back_rather_than_launching_with_none(self):
		# BlockDeviceMappings only resizes the root volume if it names the same device.
		self.assertEqual(client(images={"Architecture": "x86_64"}).get_image_info("ami-1"),
			{"root_device_name": "/dev/sda1", "cpu_architecture": "amd64"})


@patch("time.sleep")
class TestPollInstanceReady(unittest.TestCase):
	"""The bare metal bug and its fix."""

	def test_running_with_an_ip_is_not_ready_while_a_status_check_is_initializing(self, _sleep):
		# Exactly the bare metal window: EC2 says running, the address is assigned, and nothing
		# is listening. Returning here is what handed Ansible an unreachable box.
		ec2 = client(instances=[instance()], statuses=[status(system="initializing")])
		with self.assertRaises(AWSError) as caught:
			ec2.poll_instance_ready("i-1", timeout_sec=1, poll_interval_sec=0)
		self.assertIn("not reachable", str(caught.exception))

	def test_the_instance_check_alone_holds_it_back_too(self, _sleep):
		ec2 = client(instances=[instance()], statuses=[status(box="initializing")])
		with self.assertRaises(AWSError):
			ec2.poll_instance_ready("i-1", timeout_sec=1, poll_interval_sec=0)

	def test_it_returns_once_both_checks_pass(self, _sleep):
		ec2 = client(
			instances=[instance()],
			statuses=[status(system="initializing"), status(box="initializing"), status()],
		)
		ready = ec2.poll_instance_ready("i-1", timeout_sec=60, poll_interval_sec=0)
		self.assertEqual(ready["public_ip"], "1.2.3.4")
		self.assertEqual(ready["status"], "Active")

	def test_a_pending_instance_is_not_probed_for_status(self, _sleep):
		# No point asking, and it keeps the describe count down over a twenty-minute wait.
		ec2 = client(instances=[instance(state="pending", public_ip=None)], statuses=[status()])
		with self.assertRaises(AWSError):
			ec2.poll_instance_ready("i-1", timeout_sec=1, poll_interval_sec=0)
		self.assertEqual(ec2.ec2.status_calls, 0)

	def test_an_instance_that_dies_while_starting_fails_immediately(self, _sleep):
		ec2 = client(instances=[instance(state="terminated")], statuses=[status()])
		with self.assertRaises(AWSError) as caught:
			ec2.poll_instance_ready("i-1", timeout_sec=60, poll_interval_sec=0)
		self.assertIn("went terminated", str(caught.exception))

	def test_status_is_asked_for_all_instances(self, _sleep):
		# Without IncludeAllInstances the response omits anything not already running, and an
		# empty list would read the same as a passing check.
		ec2 = client(instances=[instance()], statuses=[status()])
		ec2.poll_instance_ready("i-1", timeout_sec=60, poll_interval_sec=0)
		self.assertTrue(ec2.ec2.last_status_kwargs["IncludeAllInstances"])

	def test_an_empty_status_response_is_not_ready(self, _sleep):
		self.assertFalse(client(statuses=[[]]).is_instance_reachable("i-1"))


class TestRebootTimeoutIsWiredThrough(unittest.TestCase):
	"""Inference Server raises the driver reboot's timeout for a bare metal box by passing an
	extra-var. Read the role's own files: a rename on either side is silent otherwise — Ansible
	falls back to the default and the play dies ten minutes into a twenty-minute reboot."""

	def test_the_role_defines_the_default_the_task_interpolates(self):
		defaults = yaml.safe_load((GPU_HOST_ROLE / "defaults/main.yml").read_text())
		self.assertEqual(defaults["gpu_reboot_timeout"], 600)

		tasks = yaml.safe_load((GPU_HOST_ROLE / "tasks/main.yml").read_text())
		reboots = [
			task for task in tasks
			for task in ([task] + (task.get("block") or []))
			if "ansible.builtin.reboot" in task
		]
		self.assertTrue(reboots, "the gpu_host role no longer reboots — has the driver step moved?")
		for task in reboots:
			self.assertIn("gpu_reboot_timeout", str(task["ansible.builtin.reboot"]["reboot_timeout"]))

	def test_inference_server_sends_that_same_name(self):
		source = (
			Path(__file__).parent.parent / "grove/doctype/inference_server/inference_server.py"
		).read_text()
		self.assertIn('"gpu_reboot_timeout"', source)


if __name__ == "__main__":
	unittest.main()
