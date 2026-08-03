# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The gpu_host role's data-disk selection. Pure — evaluates the role's own expressions
against captured lsblk output, no site and no box.

This is the one place in the repo that runs mkfs. Picking a disk that is in use destroys
it, so the predicate is asserted here rather than on a box: the expressions and their vars
are read out of tasks/main.yml, so the role and this file cannot drift apart.

The fixtures are shaped like real `lsblk --sort SIZE` output, which is FLAT — partitions
arrive as siblings of their disk, not nested under it, and only `pkname` ties them back.
"""

import ast
import json
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment

ROLE = Path(__file__).parent.parent.parent / "deploy/vllm/ansible/roles/gpu_host"
TASKS = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

JINJA = Environment()
JINJA.filters["from_json"] = json.loads


def find_task(tasks, name):
	for task in tasks:
		if task.get("name") == name:
			return task
		if "block" in task and (found := find_task(task["block"], name)):
			return found
	return None


SHORTLIST = find_task(TASKS, "Shortlist the disks nothing else is using")
PINNED_IS_SPARE = find_task(TASKS, "Refuse a pinned data device that is in use")[
	"ansible.builtin.assert"
]["that"]


def evaluate(expression, context):
	"""One Ansible expression, with or without its own {{ }}, back as a Python value."""
	if "{{" not in expression:
		expression = "{{ %s }}" % expression
	return ast.literal_eval(JINJA.from_string(expression).render(**context))


def shortlist(*devices):
	"""The set_fact's two lists, its `vars` resolved in order the way Ansible resolves them."""
	context = {
		**DEFAULTS,
		"lsblk_json": {"stdout": json.dumps({"blockdevices": list(devices)})},
	}
	for name, expression in SHORTLIST["vars"].items():
		context[name] = evaluate(expression, context)
	return {
		key: evaluate(expression, context)
		for key, expression in SHORTLIST["ansible.builtin.set_fact"].items()
	}


def free_disks(*devices):
	return shortlist(*devices)["gpu_free_disks"]


def is_pinnable(device_path, *devices):
	context = dict(shortlist(*devices), gpu_data_device=device_path)
	return evaluate(PINNED_IS_SPARE, context) is True


EBS = "Amazon Elastic Block Store"
INSTANCE_STORE = "Amazon EC2 NVMe Instance Storage"


def disk(name, size, fstype=None, mountpoint=None, model=None, rm=False, ro=False):
	return {
		"name": name, "path": f"/dev/{name}", "type": "disk", "size": size,
		"fstype": fstype, "mountpoint": mountpoint, "pkname": None, "model": model,
		"rm": rm, "ro": ro,
	}


def partition(name, size, parent, fstype="ext4", mountpoint=None):
	return {
		"name": name, "path": f"/dev/{name}", "type": "part", "size": size,
		"fstype": fstype, "mountpoint": mountpoint, "pkname": parent, "model": None,
		"rm": False, "ro": False,
	}


# The T4 box, as lsblk actually reports it: root on a partition, so the disk itself carries
# no filesystem and no mountpoint — pkname is the only thing that gives it away.
ROOT_PART = partition("nvme0n1p1", 7515127296, "nvme0n1", mountpoint="/")
ROOT_DISK = disk("nvme0n1", 8589934592, model=EBS)
SPARE = disk("nvme1n1", 125000000000, model=EBS)
# Local NVMe on a GPU instance: blank, unmounted and the biggest disk on the box, so every
# other rule here would hand it the weights. It is wiped on stop.
EPHEMERAL = disk("nvme2n1", 1009317314560, model=INSTANCE_STORE)
# Root straight on a bare disk, no partition table: no pkname points at it, only its own
# mountpoint does.
WHOLE_ROOT = disk("sda", 480103981056, fstype="ext4", mountpoint="/")


class TestFreeDisks(unittest.TestCase):
	def test_takes_the_spare_disk(self):
		self.assertEqual(free_disks(ROOT_PART, ROOT_DISK, SPARE), ["/dev/nvme1n1"])

	def test_never_the_root_disk(self):
		self.assertEqual(free_disks(ROOT_PART, ROOT_DISK), [], "root disk offered for mkfs")
		self.assertEqual(free_disks(WHOLE_ROOT), [], "root on a bare disk offered for mkfs")

	def test_never_a_disk_carrying_boot_or_swap(self):
		"""Any partition at all rules its disk out, mounted or not."""
		spare_looking = disk("sdb", 480103981056)
		swap = partition("sdb1", 480103981056, "sdb", fstype="swap")
		self.assertEqual(free_disks(swap, spare_looking), [])

	def test_skips_disks_that_hold_something(self):
		formatted = disk("sdb", 966367641600, fstype="xfs")
		mounted = disk("sdc", 966367641600, fstype="ext4", mountpoint="/mnt/data")
		usb = disk("sdd", 966367641600, rm=True)
		self.assertEqual(free_disks(ROOT_PART, ROOT_DISK, formatted, mounted, usb), [])

	def test_reads_removable_either_way(self):
		"""util-linux < 2.38 writes rm/ro as "0"/"1", newer as JSON booleans."""
		self.assertEqual(free_disks(disk("sdb", 100, rm="0", ro="0")), ["/dev/sdb"])
		self.assertEqual(free_disks(disk("sdb", 100, rm="1", ro="0")), [])

	def test_never_ephemeral_scratch(self):
		"""Weights are hundreds of GB — the instance store loses them on the next stop."""
		self.assertEqual(free_disks(ROOT_PART, ROOT_DISK, EPHEMERAL), [])
		self.assertEqual(
			free_disks(ROOT_PART, ROOT_DISK, EPHEMERAL, SPARE),
			["/dev/nvme1n1"],
			"took the bigger ephemeral disk over the durable one",
		)

	def test_keeps_lsblk_order_so_the_largest_is_last(self):
		"""--sort SIZE orders them; the role takes [-1], so the shortlist must not reorder."""
		small, big = disk("sdb", 107374182400), disk("sdc", 966367641600)
		self.assertEqual(free_disks(small, big)[-1], "/dev/sdc")


class TestPinnedDevice(unittest.TestCase):
	def test_allows_a_spare_disk(self):
		self.assertTrue(is_pinnable("/dev/nvme1n1", ROOT_PART, ROOT_DISK, SPARE))

	def test_allows_an_already_formatted_disk(self):
		"""Unlike the auto-pick: the operator named it, and mkfs is skipped for it."""
		formatted = disk("sdb", 966367641600, fstype="ext4")
		self.assertTrue(is_pinnable("/dev/sdb", ROOT_PART, ROOT_DISK, formatted))
		self.assertNotIn("/dev/sdb", free_disks(ROOT_PART, ROOT_DISK, formatted))

	def test_allows_ephemeral_scratch(self):
		"""Auto-pick refuses it; naming it explicitly is the operator saying they want scratch."""
		self.assertTrue(is_pinnable("/dev/nvme2n1", ROOT_PART, ROOT_DISK, EPHEMERAL))
		self.assertNotIn("/dev/nvme2n1", free_disks(ROOT_PART, ROOT_DISK, EPHEMERAL))

	def test_refuses_the_root_disk(self):
		self.assertFalse(is_pinnable("/dev/nvme0n1", ROOT_PART, ROOT_DISK, SPARE))
		self.assertFalse(is_pinnable("/dev/sda", WHOLE_ROOT))

	def test_refuses_a_partition_or_a_missing_device(self):
		self.assertFalse(is_pinnable("/dev/nvme0n1p1", ROOT_PART, ROOT_DISK, SPARE))
		self.assertFalse(is_pinnable("/dev/sdz", ROOT_PART, ROOT_DISK, SPARE))


if __name__ == "__main__":
	unittest.main()
