# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Server provisioning via Ansible (§2/§8A). Builds the Go agent, then runs the
role playbook against the server's Machine, logging to Ansible Play/Task."""

import os

import frappe

from grove.ansible import get_ansible


def _app_grove_root():
	# .../apps/grove (parent of the `grove` python package dir).
	return os.path.dirname(frappe.get_app_path("grove"))


def build_agent():
	"""Return path to gateway_service source (build happens on the proxy)."""
	return os.path.join(_app_grove_root(), "gateway_service")


def provision_machine_ansible(machine_name):
	"""Provision a Machine via Ansible: run bootstrap.yml (GPU scan, ssh keys) and
	inference-host.yml (vLLM setup). Called after cloud provisioning populates public_ip,
	or directly for on-prem machines."""
	machine = frappe.get_doc("Machine", machine_name)

	if not machine.public_ip:
		frappe.throw(f"Machine {machine_name} has no public_ip set")

	frappe.db.set_value("Machine", machine.name, "status", "Provisioning")
	frappe.db.commit()

	ansible = get_ansible()

	try:
		# Run bootstrap.yml (users, ssh keys, base packages, GPU scan)
		play_name, rc = ansible.run_playbook(
			playbook_name="bootstrap.yml",
			server_type="Machine",
			server_name=machine.name,
			machine_name=machine.name,
		)

		if rc != 0:
			frappe.db.set_value("Machine", machine.name, "status", "Broken")
			frappe.db.commit()
			return {"status": "error", "message": f"bootstrap.yml failed (rc={rc})"}

		# Run inference-host.yml (vLLM + engine setup)
		play_name, rc = ansible.run_playbook(
			playbook_name="inference-host.yml",
			server_type="Machine",
			server_name=machine.name,
			machine_name=machine.name,
		)

		if rc == 0:
			frappe.db.set_value("Machine", machine.name, "status", "Active")
		else:
			frappe.db.set_value("Machine", machine.name, "status", "Broken")

		frappe.db.commit()
		return {"status": "success" if rc == 0 else "error", "rc": rc}

	except Exception as e:
		frappe.db.set_value("Machine", machine.name, "status", "Broken")
		frappe.db.commit()
		return {"status": "error", "message": str(e)}
