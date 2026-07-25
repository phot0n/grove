# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.utils import ansible_project_dir

# Task name in scan_gpus.yml whose output carries the nvidia-smi CSV.
SCAN_TASK = "query nvidia-smi"
MIB_PER_GB = 1024


class Machine(Document):
	"""An on-prem / baremetal / VM box that Inference Servers are provisioned on. Cloud GPU
	pods are a separate, standalone Pod doctype — not backed by a Machine."""

	@frappe.whitelist()
	def scan_gpus(self):
		"""Button: read this box's real GPU inventory off nvidia-smi and rewrite the GPU
		table from it (long job — it SSHes to the box)."""
		if not self.public_ip:
			frappe.throw(f"Machine {self.name} has no public IP — nothing to connect to.")
		frappe.enqueue(
			"grove.grove.doctype.machine.machine.scan_machine_gpus",
			queue="long", timeout=600, machine_name=self.name,
		)
		frappe.msgprint(f"Scanning {self.name}'s GPUs — watch its Ansible Plays, then reload.")


def scan_machine_gpus(machine_name):
	"""Job: run scan_gpus.yml, parse nvidia-smi's CSV out of the task result, and replace the
	Machine's GPU rows with what the box actually reports. nvidia-smi is the truth here — the
	rows are rewritten rather than merged, so a swapped or removed card can't linger."""
	machine = frappe.get_doc("Machine", machine_name)
	ansible = Ansible(project_root=ansible_project_dir("machine"))
	play_name, rc = ansible.run_playbook(
		playbook_name="scan_gpus.yml",
		server_type="Machine",
		server_name=machine.name,
		machine_name=machine.name,
	)
	result = _scan_result(play_name)
	if rc != 0:
		frappe.throw(
			f"GPU scan failed on {machine.name} (Ansible Play {play_name}). "
			f"nvidia-smi said: {_scan_message(result)}"
		)

	gpus = parse_nvidia_smi(result.get("stdout"))
	if not gpus:
		frappe.throw(
			f"nvidia-smi reported no GPUs on {machine.name} — GPU rows left alone. "
			f"It said: {_scan_message(result)}"
		)
	machine.gpus = []
	for gpu in gpus:
		machine.append("gpus", gpu)
	machine.save(ignore_permissions=True)
	frappe.db.commit()
	return gpus


def _scan_result(play_name):
	"""The scan task's result, off its Ansible Task doc (the runner stores each one as JSON
	there). Empty when the task never ran — an unreachable host has no result to report."""
	output = frappe.db.get_value(
		"Ansible Task", {"play": play_name, "task_name": SCAN_TASK}, "output"
	)
	try:
		return json.loads(output) if output else {}
	except json.JSONDecodeError:
		return {}


def _scan_message(result):
	"""What nvidia-smi actually said, for an error the operator can act on without opening
	the play. Names the usual cause of the mismatch, which is by far the common failure."""
	message = (
		result.get("stdout") or result.get("stderr") or result.get("msg") or "nothing at all"
	).strip()
	if "version mismatch" in message.lower():
		message += (
			" — the loaded kernel module and the userspace driver disagree, which usually means "
			"the box needs a reboot (or an nvidia module reload) after a driver upgrade."
		)
	return message


def parse_nvidia_smi(stdout):
	"""CSV from `nvidia-smi --query-gpu=index,name,memory.total,uuid` (noheader, nounits) →
	Machine GPU rows. Memory comes back in MiB; the field is whole GB, and a card reporting
	e.g. 97887 MiB is a 96 GB card, so it rounds rather than truncates."""
	gpus = []
	for line in (stdout or "").splitlines():
		fields = [field.strip() for field in line.split(",")]
		if len(fields) < 3 or not fields[0].isdigit():
			continue  # blank line, or a warning nvidia-smi printed above the CSV
		index, name, memory_mib = fields[0], fields[1], fields[2]
		gpus.append({
			"gpu_index": int(index),
			"gpu_model": name,
			"vram_gb": round(int(memory_mib) / MIB_PER_GB) if memory_mib.isdigit() else 0,
			"gpu_uuid": fields[3] if len(fields) > 3 else "",
		})
	return gpus
