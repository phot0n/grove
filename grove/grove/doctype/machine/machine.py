# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import json
import selectors
import shlex
import subprocess

import frappe
from frappe.model.document import Document

from grove import failure
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
		for doctype in ("Gateway Server", "Inference Server"):
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
		frappe.msgprint(f"Scanning {self.name}'s GPUs — watch its Ansible Plays, then reload.", alert=True)

	@frappe.whitelist()
	def get_gpus(self):
		"""This box's cards, with whatever holds each. What the form's GPU panel renders.

		Read live off the `GPU` records rather than a stored summary, so the panel cannot drift
		from what placement sees — they run the same query."""
		from grove.grove.doctype.gpu.gpu import cards_on

		return cards_on([self.name])

	@frappe.whitelist()
	def gpu_memory(self):
		"""Button: what each GPU on this box is using right now. Nothing is stored — the
		numbers are stale the moment they arrive, so they go straight to the dialog."""
		if not self.public_ip:
			frappe.throw(f"Machine {self.name} has no public IP — nothing to connect to.")
		# ponytail: runs Ansible in the request (~10s) because the dialog needs the answer,
		# not a job id. Move to enqueue + realtime if that wait ever gets noticed.
		play_name, rc = self.run_playbook("scan_gpus.yml")
		result = _scan_result(play_name)
		if rc != 0:
			frappe.throw(
				f"Could not read GPU memory on {self.name} (Ansible Play {play_name}). "
				f"nvidia-smi said: {_scan_message(result)}"
			)
		return parse_gpu_memory(result.get("stdout"))

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
		"""This box's security groups: the Network's proxy or inference list, matching this
		Machine's Machine Type. Empty when there's no Network.

		An Ingress Server takes the proxy list with a Gateway Server. It needs the same 80/443
		open — to the gateways, which sit in no Network of their own, and to the Route53 health
		checkers behind its Network's shared name, whose addresses are AWS's to change.

		A Monitoring Agent box takes the inference list. It needs only 22 inbound — its vmagent
		and its node_exporter both bind 127.0.0.1, and its scrapes and remote writes are all
		outbound — so the proxy list would open 80/443 to the world for nothing."""
		if not network:
			return []
		if not self.machine_type:
			frappe.throw(f"Set a Machine Type on Machine {self.name} to pick its security groups.")
		return (
			network.proxy_security_group_id_list
			if self.machine_type in ("Gateway", "Ingress")
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
		frappe.msgprint(f"Provisioning {self.name} — reload when it reports Active.", alert=True)

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

	@failure.reports_failure(mark_broken=False)
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
		except Exception:
			# Back to Pending, not Broken: a Machine has no failed state, and an instance that was
			# never launched is exactly as un-launched as it was before. The comment, toast and
			# notification that used to be written here come from the decorator now — this keeps
			# only the part that is specific to launching.
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
		"""Reflect this Machine no longer serving onto every Gateway, Ingress and Inference Server
		and Monitoring Agent built on it — Active there would be a lie once the box itself is
		Terminated, Offline or Draining. Terminated propagates as Terminated (the box is gone
		for good, along with whatever was on it); Offline/Draining propagate as Broken
		(stopped, but Start can still bring it back). Only touches ones that were Active —
		Pending/Installing/Broken/Terminated already say what's true.

		An agent matters most here: it is the only doc whose silence looks exactly like a
		healthy idle fleet, so a stopped agent box has to say so on the doc.

		The Ingress Server was missing from this list until it bit: it carries a machine, a status
		and DNS records of its own, so a terminated box left one Active and still resolving, with
		nothing else in the fleet that would ever correct it."""
		if self.status not in ("Terminated", "Offline", "Draining"):
			return
		dependent_status = "Terminated" if self.status == "Terminated" else "Broken"
		for doctype in ("Gateway Server", "Ingress Server", "Inference Server", "Monitoring Agent"):
			for name in frappe.get_all(
				doctype, filters={"machine": self.name, "status": "Active"}, pluck="name"
			):
				self.mark_dependent(doctype, name, dependent_status)

	def mark_dependent(self, doctype, name, status):
		"""Write the status THROUGH the document, not with db.set_value.

		The write has consequences, and they live in on_update: a Terminated gateway or ingress has
		to give up its DNS records, and a gateway leaving also has to come out of the inference
		security groups. db.set_value skips validate and on_update entirely, so none of that ran —
		terminating a Machine left a latency record in Route53 pointing at a box that no longer
		existed, which is a black hole for every client that resolves to it.

		Guarded per document on purpose. save() runs validate, so a dependent that will not validate
		for some unrelated reason would otherwise abort the termination halfway and leave the
		machine gone with its siblings still claiming to be Active. Reported rather than swallowed,
		so a dependent that could not be updated says so."""
		try:
			doc = frappe.get_doc(doctype, name)
			doc.status = status
			doc.save(ignore_permissions=True)
		except Exception as error:
			failure.report(
				doctype,
				name,
				f"Could not mark {status.lower()} after {self.name} went {self.status.lower()}",
				str(error),
			)

	def sync_instance_type(self, client):
		"""Record what this instance type is and ships: its architecture, whether it is bare
		metal, its ephemeral local NVMe (which Grove leaves unmounted) and its GPUs. Runs at
		preflight as well as after launch — it reads the type, never the instance — so the
		architecture and the boot timeout are known before anything needs them.

		GPUs are only seeded when the box has NONE — a scanned card is nvidia-smi's answer and is
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
		from grove.grove.doctype.gpu.gpu import cards_on

		if cards_on([self.name]):
			return
		reconcile_gpus(self.name, info["gpus"])
		frappe.db.commit()

	@frappe.whitelist()
	def stop(self):
		"""Button: stop the instance. The root volume (images and weights) survives, and so does
		the address when it's an Elastic IP — a dynamic one is gone the moment it stops."""
		self.require_instance()
		self.cloud_client.stop_instance(self.instance_id)
		self.db_set({"status": "Draining", "public_ip": self.public_ip if self.is_static_ip else ""})
		self.sync_dependent_servers()
		frappe.msgprint(f"Stopping {self.name} — Sync once it settles.", alert=True)

	@frappe.whitelist()
	def start(self):
		"""Button: start a stopped instance. AWS re-issues the public IP, so this polls for
		the new one rather than leaving the old, now-wrong address on the doc."""
		self.require_instance()
		self.cloud_client
		frappe.enqueue_doc(self.doctype, self.name, "resume", queue="long", timeout=3000)
		frappe.msgprint(f"Starting {self.name} — reload when it reports Active.", alert=True)

	@failure.reports_failure(mark_broken=False)
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
		frappe.msgprint(f"Growing {self.name}'s root volume to {size_gb} GB — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=False)
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
		frappe.msgprint(f"Terminated {self.name}.", alert=True)

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
	"""Job: run scan_gpus.yml and reconcile this Machine's `GPU` records against what the box
	reports. nvidia-smi is the truth about which cards exist."""
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
			f"nvidia-smi reported no GPUs on {machine.name} — GPU records left alone. "
			f"It said: {_scan_message(result)}"
		)
	reconcile_gpus(machine.name, gpus)
	frappe.db.commit()
	return gpus


def is_placeholder_device_id(device_id):
	"""Whether this id is a bare CUDA index standing in for a UUID nothing has read yet.

	Written by `promote_gpus` for a legacy row and by `aws.py` at provision, where no driver has
	been asked. It says which slot the card sat in, never which card it is."""
	return (device_id or "").strip().isdigit()


def plan_reconcile(existing, scanned, slot_is_identity=False):
	"""What the scan means for the cards already on record: `(upgrades, inserts, stale)`.

	Pure, so the rule can be pinned without a site — and it is worth pinning, because getting it
	wrong deletes a row that a running replica's claim lives on.

	Two passes. A real `device_id` matches only itself, so two cards that swapped slots keep their
	own rows. A PLACEHOLDER matches by slot instead: it was never an identity, so a scan reporting
	a real UUID for that slot is the same card seen properly for the first time, and upgrading it
	in place is what keeps its `held_by` and the replica rows pointing at it.

	`slot_is_identity` widens that second pass to every card, and is for a box whose hardware is
	rented. Stopping and starting an EC2 instance migrates it to another host — AWS's own advice
	for a sick GPU is to do exactly that — so nvidia-smi comes back with a different physical card
	at the same index. The UUID that was identity yesterday names silicon this account no longer
	has, while the slot is what persists. On bare metal the opposite holds, which is why this is a
	property of the box and not a global rule: there, a card that stops answering has been pulled,
	and pruning it is the honest reading."""
	by_device = {card.device_id: card for card in existing}
	upgrades, inserts, unmatched = [], [], []
	claimed = set()

	for row in scanned:
		card = by_device.get(row["device_id"])
		if card and card.name not in claimed:
			upgrades.append((card, row))
			claimed.add(card.name)
		else:
			unmatched.append(row)

	by_slot = {
		card.gpu_index: card
		for card in existing
		if card.name not in claimed
		and (slot_is_identity or is_placeholder_device_id(card.device_id))
	}
	for row in unmatched:
		card = by_slot.pop(row.get("gpu_index"), None)
		if card:
			upgrades.append((card, row))
			claimed.add(card.name)
		else:
			inserts.append(row)

	return upgrades, inserts, [card for card in existing if card.name not in claimed]


def reconcile_gpus(machine, scanned):
	"""Upsert one `GPU` per scanned card and prune what the box no longer reports.

	Upserted rather than replaced. A card that is still there keeps its row — and therefore keeps
	its `held_by`, so a re-scan of a busy box does not disturb a running replica. Rewriting the
	list, which is what this used to do, silently dropped the claim with the row."""
	from grove.grove.doctype.gpu.gpu import cards_on

	existing = cards_on([machine])
	# A rented box identifies its cards by SLOT: a stop/start lands the instance on another host,
	# so the same index answers with a different UUID and the old one names nothing. Pruning there
	# would delete a row a replica still points at, for a card that never went anywhere.
	slot_is_identity = bool(frappe.db.get_value("Machine", machine, "cloud_provider"))
	upgrades, inserts, stale = plan_reconcile(existing, scanned, slot_is_identity)

	for card, row in upgrades:
		frappe.db.set_value(
			"GPU",
			card.name,
			{"device_id": row["device_id"], **_scanned_values(row)},
			update_modified=False,
		)
	for row in inserts:
		frappe.get_doc(
			{
				"doctype": "GPU",
				"machine": machine,
				"device_id": row["device_id"],
				**_scanned_values(row),
			}
		).insert(ignore_permissions=True)
	for card in stale:
		# The card is gone, so whatever held it is holding nothing. Deleting the row takes the
		# claim with it, which is the point of the claim being a column here.
		frappe.delete_doc("GPU", card.name, ignore_permissions=True, force=True)

	_refresh_from_types(machine)
	_mirror_onto_machine(machine)


def _scanned_values(row):
	"""The columns a scan owns on a card. Its type is resolved, not stored as the reported string."""
	from grove.grove.doctype.gpu_type.gpu_type import resolve

	return {
		"gpu_type": resolve(
			row.get("gpu_model"), row.get("vram_gb"), row.get("compute_capability")
		),
		"gpu_index": row.get("gpu_index") or 0,
	}


def _is_number(value):
	try:
		float(value)
	except (TypeError, ValueError):
		return False
	return True


def _refresh_from_types(machine):
	"""Re-pull what the cards fetch off their type.

	A scan writes cards with `db.set_value`, which sets a link without running its fetches — so a
	card whose type was seeded or corrected in this same scan would keep the old figure, and the
	placement checks read the CARD. `GPU Type.on_update` covers an operator editing the type; this
	covers the scan that taught it."""
	frappe.db.sql(
		"""update `tabGPU` gpu join `tabGPU Type` type on type.name = gpu.gpu_type
		set gpu.vram_gb = type.vram_gb, gpu.compute_capability = type.compute_capability
		where gpu.machine = %(machine)s""",
		{"machine": machine},
	)


def _mirror_onto_machine(machine):
	"""Rewrite the Machine's GPU grid to match its cards.

	The grid is a read-only mirror, not a second source of truth: `GPU.machine` is what says where
	a card is, and this only exists so the Machine form can show them as a grid with links rather
	than a rendered panel. Rewritten by the same function that reconciles the cards, so there is
	one writer and the two cannot disagree.

	Everything but the link is `fetch_from` the GPU, so a card renamed or re-typed later shows
	through without this running again."""
	from grove.grove.doctype.gpu.gpu import cards_on

	doc = frappe.get_doc("Machine", machine)
	doc.gpus = []
	for card in cards_on([machine]):
		doc.append("gpus", {"gpu": card.name})
	doc.save(ignore_permissions=True)


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
	scanned cards. Memory comes back in MiB; the field is whole GB, and a card reporting
	e.g. 97887 MiB is a 96 GB card, so it rounds rather than truncates."""
	gpus = []
	for line in (stdout or "").splitlines():
		fields = [field.strip() for field in line.split(",")]
		if len(fields) < 3 or not fields[0].isdigit():
			continue  # blank line, or a warning nvidia-smi printed above the CSV
		index, name, memory_mib = fields[0], fields[1], fields[2]
		uuid = fields[3] if len(fields) > 3 else ""
		# Appended to the query rather than inserted, so a box answering an older playbook still
		# parses — and so parse_gpu_memory's column offsets do not move.
		compute_cap = fields[6] if len(fields) > 6 else ""
		gpus.append({
			"gpu_index": int(index),
			"gpu_model": name,
			"vram_gb": vram_gb_from_mib(int(memory_mib)) if memory_mib.isdigit() else 0,
			# What CUDA_VISIBLE_DEVICES is given. The UUID when the box reported one, so the card
			# keeps its identity across a reseat; the index only as a fallback for a driver that
			# answered without one.
			"device_id": uuid or str(int(index)),
			# What says whether this card can run bfloat16 at all. Blank on a driver too old to
			# report it, which reads as "unknown" and skips the check rather than failing it.
			"compute_capability": float(compute_cap) if _is_number(compute_cap) else 0,
		})
	return gpus


def parse_gpu_memory(stdout):
	"""The same CSV read for its transient columns — total/used/free MiB per card. A card
	that reports a non-numeric figure (MIG, a driver that answers [N/A]) is skipped rather
	than shown as 0, which would read as an idle GPU."""
	rows = []
	for line in (stdout or "").splitlines():
		fields = [field.strip() for field in line.split(",")]
		if len(fields) < 6 or not fields[0].isdigit():
			continue
		total, used, free = fields[2], fields[4], fields[5]
		if not (total.isdigit() and used.isdigit() and free.isdigit()):
			continue
		rows.append({
			"gpu_index": int(fields[0]),
			"gpu_model": fields[1],
			"total_mib": int(total),
			"used_mib": int(used),
			"free_mib": int(free),
		})
	return rows
