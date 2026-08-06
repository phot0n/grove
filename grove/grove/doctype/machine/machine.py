# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import selectors
import shlex
import subprocess

import frappe
from frappe.model.document import Document

from grove.ansible import AnsibleHost
from grove.cloud_provider.base import CloudClientError, build_cloud_client
from grove.grove.doctype.ssh_key.ssh_key import injected_public_keys
from grove.utils import vram_gb_from_mib

# Task name in scan_gpus.yml whose output carries the nvidia-smi CSV.
SCAN_TASK = "query nvidia-smi"


class Machine(AnsibleHost, Document):
	"""An on-prem / baremetal / VM box that Inference Servers are provisioned on. Cloud GPU
	pods are a separate, standalone Pod doctype — not backed by a Machine."""

	@property
	def playbook_machine(self):
		"""This doc IS the box — a server doc links one, a Machine names itself."""
		return self.name

	def validate(self):
		if self.network:
			region = frappe.db.get_value("Network", self.network, "region")
			if region:
				self.region = region
		self.validate_launch_locked_fields()

	def validate_launch_locked_fields(self):
		"""The SSH keys go in via user-data at launch, so editing the key afterwards would only
		make this doc disagree with the box."""
		if self.is_new() or not self.instance_id:
			return
		if self.has_value_changed("ssh_key"):
			frappe.throw(
				f"SSH Key cannot change once {self.name} is launched (instance "
				f"{self.instance_id}). Terminate and relaunch to change it."
			)

	def on_update(self):
		if self.has_value_changed("is_static_ip"):
			self.sync_static_ip()

	def sync_static_ip(self):
		"""Mirror the static-IP flag onto the servers on this box. The Machine owns the address,
		so the flag is set here and the servers only report it."""
		for doctype in ("Proxy Server", "Inference Server"):
			for name in frappe.get_all(doctype, filters={"machine": self.name}, pluck="name"):
				frappe.db.set_value(doctype, name, "is_static_ip", self.is_static_ip)

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

	# ── Cloud provisioning ────────────────────────────────────────────────────

	@property
	def cloud_client(self):
		"""CloudClient for this box: its Cloud Provider's account keys and kind, its Region's
		code. Never a concrete class by name — build_cloud_client is the one place that picks
		one from the Cloud Provider's provider_type."""
		if not self.cloud_provider:
			frappe.throw(
				f"Machine {self.name} has no Cloud Provider — set one, "
				f"or leave it blank for an on-prem box."
			)
		provider = frappe.get_doc("Cloud Provider", self.cloud_provider)
		secret = provider.get_password("api_key", raise_exception=False)
		if not (provider.access_key_id and secret):
			frappe.throw(f"Cloud Provider {provider.name} has no credentials set.")
		try:
			return build_cloud_client(provider.provider_type, provider.access_key_id, secret, self.region_code)
		except CloudClientError as e:
			frappe.throw(str(e))

	@property
	def region_code(self):
		"""The region to talk to — this box's Region, whose Name is the provider's own region
		code. No account-wide fallback: every AWS Machine needs a Region, directly or via its
		Network."""
		if not self.region:
			frappe.throw(f"Machine {self.name} has no Region set — link a Network, or set one directly.")
		return self.region

	@property
	def resolved_machine_image(self):
		"""This Machine's own Machine Image, else its Network's — AMI ids are region-scoped,
		so the default lives on Network (one region each), not on Cloud Provider (one account,
		many regions)."""
		network = self.network_doc
		value = self.machine_image or (network.machine_image if network else "")
		if not value:
			suffix = f" or on Network {network.name}" if network else ""
			frappe.throw(f"Set Machine Image on Machine {self.name}{suffix}.")
		return value

	@property
	def network_doc(self):
		"""This Machine's Network. None for on-prem, or an AWS box with no Network linked."""
		return frappe.get_cached_doc("Network", self.network) if self.network else None

	def get_security_group_ids(self, network):
		"""This box's security groups: the Network's Proxy Server or Inference Server list,
		matching this Machine's Machine Type. Empty when there's no Network.

		A Monitoring Agent box takes the inference list. It needs only 22 inbound — its vmagent
		and its node_exporter both bind 127.0.0.1, and its scrapes and remote writes are all
		outbound — so the proxy list would open 80/443 to the world for nothing."""
		if not network:
			return []
		if not self.machine_type:
			frappe.throw(f"Set a Machine Type on Machine {self.name} to pick its security groups.")
		return (
			network.proxy_security_group_id_list
			if self.machine_type == "Proxy Server"
			else network.inference_security_group_id_list
		)

	@property
	def boot_timeout_sec(self):
		"""How long this box may take to become reachable. A bare metal instance POSTs real
		firmware before anything listens, on a launch and on every start after one."""
		return 2400 if self.is_bare_metal else 900

	@frappe.whitelist()
	def provision(self):
		"""Button: launch this box on EC2. Everything that can be checked without a running
		instance is checked here — credentials, region, image, security groups, and the instance
		type — so a typo fails on the button rather than after AWS has started billing."""
		if self.instance_id:
			frappe.throw(f"Machine {self.name} already has instance {self.instance_id}.")
		if not (self.instance_type and self.root_volume_gb):
			frappe.throw("Set an Instance Type and a Root Volume (GB) before provisioning.")
		client = self.cloud_client
		self.get_security_group_ids(self.network_doc)
		self.sync_instance_type(client)
		self.validate_image_architecture(client)
		frappe.enqueue_doc(self.doctype, self.name, "launch", queue="long", timeout=3000)
		frappe.msgprint(f"Provisioning {self.name} — reload when it reports Active.")

	def validate_image_architecture(self, client):
		"""Refuse an AMI built for a different architecture than the instance type runs. AWS
		accepts the launch and the box then never boots — there is no console to read and no
		status check that ever passes, only a twenty-minute wait ending in a timeout."""
		image = self.resolved_machine_image
		image_architecture = client.get_image_info(image)["cpu_architecture"]
		if self.cpu_architecture and image_architecture != self.cpu_architecture:
			frappe.throw(
				f"AMI {image} is {image_architecture}, but {self.instance_type} runs "
				f"{self.cpu_architecture}. Set a {self.cpu_architecture} Machine Image on "
				f"{self.name}, or on its Network."
			)

	def launch(self):
		"""Job: run the instance, wait for it to be reachable, then record what AWS gave back
		and seed the GPU table from the instance type. A failure before instance_id is set
		(bad AMI, credentials, quota, ...) resets status to Pending rather than stranding the
		Machine at Provisioning forever — the UI only offers Provision again once it's back."""
		self.db_set("status", "Provisioning", commit=True)
		try:
			client = self.cloud_client
			network = self.network_doc
			instance = client.run_instance(
				name=self.name,
				instance_type=self.instance_type,
				image_id=self.resolved_machine_image,
				subnet_id=network.subnet_id if network else "",
				security_group_ids=self.get_security_group_ids(network),
				root_volume_gb=self.root_volume_gb,
				ssh_public_keys=injected_public_keys(),
			)
		except Exception as e:
			# On the doc, not only in the job's Error Log: InsufficientInstanceCapacity is the
			# ordinary answer for a GPU type, and an operator watching this snap back to Pending
			# has nothing else to read.
			self.add_comment("Comment", f"Provision failed: {e}")
			self.db_set("status", "Pending", commit=True)
			raise
		# Committed before the poll: a timeout further down must not roll this back and leave
		# a billed instance that Grove has no record of.
		self.db_set("instance_id", instance["instance_id"], commit=True)
		ready = client.poll_instance_ready(instance["instance_id"], timeout_sec=self.boot_timeout_sec)
		self.db_set({
			"public_ip": ready["public_ip"],
			"private_ip": ready["private_ip"],
			"status": ready["status"],
		}, commit=True)
		if self.is_static_ip:
			self.attach_static_ip()
		self.sync_instance_type(client)

	@frappe.whitelist()
	def attach_static_ip(self):
		"""Button: swap this box's address for an Elastic IP, which survives a Stop. Runs at
		launch when the box asked for one, and on a running box whenever an operator wants a
		stable address. Recorded with its allocation id — that, not the address, is what hands
		it back later."""
		self.require_instance()
		if self.static_ip_allocation_id:
			frappe.throw(f"{self.name} already holds Elastic IP {self.public_ip}.")
		address = self.cloud_client.allocate_static_ip(self.instance_id)
		self.db_set({
			"public_ip": address["public_ip"],
			"static_ip_allocation_id": address["allocation_id"],
			"is_static_ip": 1,
		}, commit=True)
		self.sync_static_ip()

	@frappe.whitelist()
	def release_static_ip(self):
		"""Button: hand the Elastic IP back — it is billed for as long as it is held. AWS gives a
		running box a fresh dynamic address the moment the Elastic IP comes off, so the address
		is re-read off the instance rather than guessed at."""
		if not self.static_ip_allocation_id:
			frappe.throw(f"{self.name} has no Elastic IP to release.")
		self.cloud_client.release_static_ip(self.static_ip_allocation_id)
		self.db_set({"static_ip_allocation_id": "", "is_static_ip": 0}, commit=True)
		self.sync_static_ip()
		self.sync()

	@frappe.whitelist()
	def sync(self):
		"""Button: pull state, IPs and launch facts back off AWS — instance_type, machine_image
		and root_volume_gb included, so a box registered by hand (instance_id set, nothing
		else) gets fully backfilled from what's actually running."""
		self.require_instance()
		client = self.cloud_client
		try:
			instance = client.get_instance(self.instance_id)
		except CloudClientError as e:
			if e.code != "InvalidInstanceID.NotFound":
				raise
			self.db_set({"status": "Terminated", "instance_id": "", "public_ip": "", "private_ip": ""})
			self.sync_dependent_servers()
			frappe.msgprint(f"{self.name} no longer exists on AWS — marked Terminated.")
			return
		self.db_set({
			"status": instance["status"],
			"public_ip": instance["public_ip"] or "",
			"private_ip": instance["private_ip"] or "",
			"instance_type": instance["instance_type"] or "",
			"machine_image": instance["image_id"] or "",
			"root_volume_gb": instance["root_volume_gb"] or 0,
		})
		self.sync_dependent_servers()
		self.sync_instance_type(client)

	def sync_dependent_servers(self):
		"""Reflect this Machine no longer serving onto every Proxy/Inference Server and
		Monitoring Agent built on it — Active there would be a lie once the box itself is
		Terminated, Offline or Draining. Terminated propagates as Terminated (the box is gone
		for good, along with whatever was on it); Offline/Draining propagate as Broken
		(stopped, but Start can still bring it back). Only touches ones that were Active —
		Pending/Installing/Broken/Terminated already say what's true.

		An agent matters most here: it is the only doc whose silence looks exactly like a
		healthy idle fleet, so a stopped agent box has to say so on the doc."""
		if self.status not in ("Terminated", "Offline", "Draining"):
			return
		dependent_status = "Terminated" if self.status == "Terminated" else "Broken"
		for doctype in ("Proxy Server", "Inference Server", "Monitoring Agent"):
			for name in frappe.get_all(
				doctype, filters={"machine": self.name, "status": "Active"}, pluck="name"
			):
				frappe.db.set_value(doctype, name, "status", dependent_status)

	def sync_instance_type(self, client):
		"""Record what this instance type is and ships: its architecture, whether it is bare
		metal, its ephemeral local NVMe (which Grove leaves unmounted) and its GPUs. Runs at
		preflight as well as after launch — it reads the type, never the instance — so the
		architecture and the boot timeout are known before anything needs them.

		The GPU table is only seeded when EMPTY — a scanned table is nvidia-smi's answer and is
		never overwritten by the provider's coarser one."""
		if not self.instance_type:
			return
		info = client.get_instance_type_info(self.instance_type)
		store = info["instance_store"]
		self.db_set({
			"cpu_architecture": info["cpu_architecture"],
			"is_bare_metal": int(info["is_bare_metal"]),
			"instance_store_disks": store["disks"],
			"instance_store_gb": store["total_gb"],
		})
		if self.gpus:
			return
		for gpu in info["gpus"]:
			self.append("gpus", gpu)
		self.save(ignore_permissions=True)
		frappe.db.commit()

	@frappe.whitelist()
	def stop(self):
		"""Button: stop the instance. The root volume (images and weights) survives, and so does
		the address when it's an Elastic IP — a dynamic one is gone the moment it stops."""
		self.require_instance()
		self.cloud_client.stop_instance(self.instance_id)
		self.db_set({"status": "Draining", "public_ip": self.public_ip if self.is_static_ip else ""})
		self.sync_dependent_servers()
		frappe.msgprint(f"Stopping {self.name} — Sync once it settles.")

	@frappe.whitelist()
	def start(self):
		"""Button: start a stopped instance. AWS re-issues the public IP, so this polls for
		the new one rather than leaving the old, now-wrong address on the doc."""
		self.require_instance()
		self.cloud_client
		frappe.enqueue_doc(self.doctype, self.name, "resume", queue="long", timeout=3000)
		frappe.msgprint(f"Starting {self.name} — reload when it reports Active.")

	def resume(self):
		"""Job: start the instance and record the address AWS gives it this time. A start costs
		the same firmware POST a launch does, so it waits as long."""
		client = self.cloud_client
		client.start_instance(self.instance_id)
		ready = client.poll_instance_ready(self.instance_id, timeout_sec=self.boot_timeout_sec)
		self.db_set({
			"public_ip": ready["public_ip"],
			"private_ip": ready["private_ip"],
			"status": ready["status"],
		}, commit=True)

	@frappe.whitelist()
	def resize_root_volume(self, size_gb: int):
		"""Button: grow the only disk this box has. The root volume holds the OS, the engine
		images and every weight, so a box provisioned too small cannot serve at all — and
		relaunching it to fix that throws away everything already downloaded."""
		self.require_instance()
		if size_gb <= (self.root_volume_gb or 0):
			frappe.throw(
				f"{self.name}'s root volume is already {self.root_volume_gb} GB. "
				"A volume can only be grown, never shrunk."
			)
		self.cloud_client
		frappe.enqueue_doc(
			self.doctype, self.name, "grow_root", queue="long", timeout=1800, size_gb=size_gb
		)
		frappe.msgprint(f"Growing {self.name}'s root volume to {size_gb} GB — watch its Ansible Plays.")

	def grow_root(self, size_gb):
		"""Job: enlarge the volume at the provider, then grow the partition and filesystem on
		the box. root_volume_gb is written only once the box reports the space, so the field
		never claims a size the filesystem does not have."""
		self.cloud_client.resize_root_volume(self.instance_id, size_gb)
		play_name, rc = self.run_playbook("grow_root.yml")
		if rc != 0:
			frappe.throw(
				f"{self.name}'s root volume is now {size_gb} GB at AWS, but growing the "
				f"filesystem onto it failed (Ansible Play {play_name})."
			)
		self.db_set("root_volume_gb", size_gb, commit=True)

	@frappe.whitelist()
	def terminate(self):
		"""Button: destroy the instance. The root volume goes with it — weights and all."""
		self.require_instance()
		client = self.cloud_client
		if self.static_ip_allocation_id:
			# Handed back first: an Elastic IP is billed while allocated, and once the instance
			# is gone this doc is the only record of which address to release.
			client.release_static_ip(self.static_ip_allocation_id)
			self.db_set("static_ip_allocation_id", "")
		client.terminate_instance(self.instance_id)
		self.db_set({"status": "Terminated", "instance_id": "", "public_ip": "", "private_ip": ""})
		self.sync_dependent_servers()
		frappe.msgprint(f"Terminated {self.name}.")

	def require_instance(self):
		if not self.instance_id:
			frappe.throw(f"Machine {self.name} has no EC2 instance — provision it first.")

	def get_ssh_argv(self, command, tty=False):
		"""The local `ssh` argv that runs one remote argv on this box.

		The remote argv is quoted word by word, so a unit name or a slug cannot become a second
		command on the far side. Not root → sudo -n, which fails loudly rather than hanging on
		a password prompt. `tty` forces a pty: without one, killing the local ssh leaves the
		remote command running (sshd only hangs it up when it next writes), so anything that
		follows output needs it to avoid leaking a process on the box per call."""
		if not self.public_ip:
			frappe.throw(f"Machine {self.name} has no public IP — nothing to connect to.")
		user = self.ssh_user or "root"
		if user != "root":
			command = ["sudo", "-n", *command]
		remote = " ".join(shlex.quote(word) for word in command)
		return [
			"ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
			"-o", "ConnectTimeout=10", "-p", str(self.ssh_port or 22),
			*(["-tt"] if tty else []),
			f"{user}@{self.public_ip}", remote,
		]

	def run_command(self, command, timeout=60):
		"""Run one argv on this box over SSH and return what it printed (stdout + stderr).
		For reads that are not worth a playbook — a log tail, a status — where an Ansible
		Play doc per call would be noise. Anything that changes the box belongs in a role."""
		try:
			result = subprocess.run(
				self.get_ssh_argv(command), capture_output=True, text=True, timeout=timeout
			)
		except subprocess.TimeoutExpired:
			frappe.throw(f"{self.name} did not answer within {timeout}s.")
		return (result.stdout + result.stderr).strip()

	def stream_command(self, command, idle_tick=5):
		"""Follow one argv on this box over SSH, yielding its output line by line as it arrives
		— the follow-mode twin of run_command. Yields None after each idle_tick seconds of
		silence, so a caller can act (stop, flush) without waiting for the next line.

		Runs on a pty (see get_ssh_argv) so that killing the local ssh hangs the remote command
		up too — a caller that stops consuming must not leave a `docker logs -f` on the box."""
		process = subprocess.Popen(
			self.get_ssh_argv(command, tty=True),
			stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
		)
		try:
			selector = selectors.DefaultSelector()
			selector.register(process.stdout, selectors.EVENT_READ)
			while True:
				if not selector.select(timeout=idle_tick):
					yield None
					continue
				line = process.stdout.readline()
				if not line:  # EOF — the remote command ended
					return
				yield line.rstrip("\r\n")  # the pty ends lines \r\n
		finally:
			process.kill()


def scan_machine_gpus(machine_name):
	"""Job: run scan_gpus.yml, parse nvidia-smi's CSV out of the task result, and replace the
	Machine's GPU rows with what the box actually reports. nvidia-smi is the truth here — the
	rows are rewritten rather than merged, so a swapped or removed card can't linger."""
	machine = frappe.get_doc("Machine", machine_name)
	play_name, rc = machine.run_playbook("scan_gpus.yml")
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
			"vram_gb": vram_gb_from_mib(int(memory_mib)) if memory_mib.isdigit() else 0,
			"gpu_uuid": fields[3] if len(fields) > 3 else "",
		})
	return gpus
