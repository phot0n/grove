# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Machine provisioning via cloud provider APIs. Called from Machine.setup() when
cloud_provider is set (e.g. runpod). Spawns a pod with a direct-TCP port pool + injected
SSH keys, reads the public IP + port map back, fills GPU rows, and stands up an
Inference Server on the box ready for model deploys."""

import json

import frappe

from grove.cloud_provider.runpod import RunPodClient, RunPodError
from grove.grove.doctype.ssh_key.ssh_key import injected_public_keys

# The pod's local volume disk mounts here (persists across restart). Shared with serve:
# vLLM home lives under this path so venvs/weights survive (see model_deployment).
VOLUME_MOUNT = "/data"

# vLLM TCP ports to pre-open at spawn (8080..). RunPod can't hot-add ports, so this fixed
# pool caps concurrent model deployments on a pod (bounded by VRAM in practice anyway).
ENGINE_PORT_POOL_SIZE = 5


def provision_machine(machine_name):
	"""Provision a Machine via its cloud_provider API. Spawns the pod, populates
	networking/GPU/port_map, then stands up an Inference Server on it."""
	machine = frappe.get_doc("Machine", machine_name)
	if not machine.cloud_provider:
		return {"status": "error", "message": "Machine has no cloud_provider set"}

	provider = frappe.get_doc("Cloud Provider", machine.cloud_provider)
	if provider.provider_type != "runpod":
		return {"status": "error", "message": f"Unsupported provider: {provider.provider_type}"}
	return _provision_runpod(machine, provider)


def _client(provider):
	api_key = provider.get_password("api_key", raise_exception=False)
	if not api_key:
		frappe.throw(f"Cloud Provider {provider.name} has no API key set.")
	return RunPodClient(api_key)


def _provision_runpod(machine, provider):
	client = _client(provider)

	pubkeys = injected_public_keys()
	if not pubkeys:
		frappe.throw(
			"No active SSH Key found — add one (the control-plane's public key) so "
			"Ansible can connect to the pod."
		)

	# GPU type + count come from the declared GPU rows (one row per GPU, gpu_model = the
	# provider's GPU id). Assumes a homogeneous pod (all rows same model).
	if not machine.gpus:
		frappe.throw(
			"Add at least one GPU row (gpu_model = provider GPU id, e.g. 'NVIDIA L40S') "
			"before provisioning a cloud machine."
		)
	gpu_type_id = machine.gpus[0].gpu_model
	gpu_count = len(machine.gpus)
	if not gpu_type_id:
		frappe.throw("GPU row is missing gpu_model (the provider's GPU id).")

	ports = client.build_ports(ENGINE_PORT_POOL_SIZE)

	try:
		frappe.db.set_value("Machine", machine.name, "status", "Provisioning")
		frappe.db.commit()

		pod = client.spawn_pod(
			gpu_type_id=gpu_type_id,
			gpu_count=gpu_count,
			volume_in_gb=machine.disk_size_gb or 50,
			ports=ports,
			env={"PUBLIC_KEY": pubkeys},
			image_name=machine.get("image_name"),
			volume_mount_path=VOLUME_MOUNT,
			name=machine.name,
		)

		# Record the pod id immediately so a later poll failure is still recoverable.
		frappe.db.set_value(
			"Machine", machine.name,
			{"cloud_instance_id": pod["pod_id"], "status": "Provisioning"},
		)
		frappe.db.commit()

		ready = client.poll_pod_ready(pod["pod_id"])

		frappe.db.set_value("Machine", machine.name, {
			"public_ip": ready["public_ip"],
			"ssh_port": ready["ssh_port"],
			"ssh_user": "root",
			"port_map": json.dumps(ready["port_map"]),
			"status": "Active",
		})
		frappe.db.commit()

		inf = _ensure_inference_server(machine.name, ready["public_ip"], machine.region)

		return {
			"status": "success",
			"pod_id": pod["pod_id"],
			"public_ip": ready["public_ip"],
			"inference_server": inf,
		}

	except RunPodError as e:
		frappe.db.set_value("Machine", machine.name, "status", "Offline")
		frappe.db.commit()
		return {"status": "error", "message": str(e)}


def _ensure_inference_server(machine_name, machine_ip, region):
	"""Create (or refresh) the Inference Server on this Machine, provisioned and ready
	for model deploys. RunPod images already ship NVIDIA driver + CUDA and the volume is
	mounted, so we skip the gpu_host provision.yml and mark is_provisioned directly."""
	existing = frappe.get_all("Inference Server", filters={"machine": machine_name}, pluck="name")
	if existing:
		frappe.db.set_value("Inference Server", existing[0], {
			"machine_ip": machine_ip, "status": "Active", "is_provisioned": 1,
		})
		frappe.db.commit()
		return existing[0]

	inf = frappe.get_doc({
		"doctype": "Inference Server",
		"machine": machine_name,
		"machine_ip": machine_ip,
		"region": region,
		"status": "Active",
		"is_provisioned": 1,
	})
	inf.name = f"inf-{machine_name}"  # autoname is prompt → set explicitly
	inf.insert(ignore_permissions=True)
	frappe.db.commit()
	return inf.name


def recover_machine(machine_name):
	"""After a pod restart: re-read the (possibly moved) public IP + port map, refresh
	the Machine + its Inference Server, then re-run serve (unit-only, skip heavy) for each
	Active deployment — a RunPod restart wipes the ephemeral root, so the systemd units
	are gone even though venvs/weights persist on the /data volume."""
	machine = frappe.get_doc("Machine", machine_name)
	if not machine.cloud_provider or not machine.cloud_instance_id:
		return {"status": "error", "message": "No cloud pod to recover"}

	provider = frappe.get_doc("Cloud Provider", machine.cloud_provider)
	client = _client(provider)
	try:
		ready = client.poll_pod_ready(machine.cloud_instance_id)
	except RunPodError as e:
		return {"status": "error", "message": str(e)}

	frappe.db.set_value("Machine", machine.name, {
		"public_ip": ready["public_ip"],
		"ssh_port": ready["ssh_port"],
		"ssh_user": "root",
		"port_map": json.dumps(ready["port_map"]),
		"status": "Active",
	})
	frappe.db.commit()

	redeployed = []
	for inf in frappe.get_all("Inference Server", filters={"machine": machine.name}, pluck="name"):
		frappe.db.set_value(
			"Inference Server", inf, {"machine_ip": ready["public_ip"], "is_provisioned": 1, "status": "Active"}
		)
		frappe.db.commit()
		for md in frappe.get_all(
			"Model Deployment", filters={"inference_server": inf, "status": "Active"}, pluck="name"
		):
			# reconfigure = serve.yml skip_tags=heavy → reinstalls the wiped systemd unit
			# and restarts; venv + weights already on /data, so no pip / re-download.
			frappe.enqueue(
				"grove.grove.doctype.model_deployment.model_deployment.reconfigure_deployment",
				queue="long",
				timeout=1200,
				model_deployment=md,
			)
			redeployed.append(md)
	return {"status": "success", "redeployed": redeployed}


def deprovision_machine(machine_name):
	"""Terminate the Machine's pod (frees GPU/disk/billing) and mark it Offline. Its
	Inference Server is marked unprovisioned — deployments on it go unreachable."""
	machine = frappe.get_doc("Machine", machine_name)
	if not machine.cloud_provider or not machine.cloud_instance_id:
		return {"status": "error", "message": "No cloud pod to terminate"}

	provider = frappe.get_doc("Cloud Provider", machine.cloud_provider)
	client = _client(provider)
	try:
		client.terminate_pod(machine.cloud_instance_id)
	except RunPodError as e:
		return {"status": "error", "message": str(e)}

	frappe.db.set_value("Machine", machine.name, {"status": "Offline", "cloud_instance_id": ""})
	for inf in frappe.get_all("Inference Server", filters={"machine": machine.name}, pluck="name"):
		frappe.db.set_value("Inference Server", inf, {"status": "Broken", "is_provisioned": 0})
	frappe.db.commit()
	return {"status": "success"}
