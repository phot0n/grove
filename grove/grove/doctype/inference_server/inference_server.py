# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document

from grove import failure
from grove.fleet import FleetHost
from grove.grove.doctype.gpu.gpu import cards_on
from grove.monitoring import run_exporters_play


class InferenceServer(FleetHost, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		data_path: DF.Data
		geography: DF.Link | None
		ingress: DF.Link | None
		is_provisioned: DF.Check
		is_standalone: DF.Check
		is_static_ip: DF.Check
		machine: DF.Link
		machine_ip: DF.Data | None
		monitoring_agent: DF.Link | None
		network: DF.Link | None
		private_ip: DF.Data | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
		use_instance_store_for_hf_cache: DF.Check
	# end: auto-generated types

	dns_fields = ("machine_ip",)

	def validate(self):
		if self.is_standalone and self.ingress:
			frappe.throw("A Standalone box takes no Ingress Server — clear one of the two.")
		self.validate_standalone_is_fixed()
		self.validate_ingress_network()
		self.validate_instance_store()

	def validate_standalone_is_fixed(self):
		"""A set-up box keeps its choice: its replicas' URLs and DNS record depend on the flag."""
		before = self.get_doc_before_save()
		if before and before.is_provisioned and before.is_standalone != self.is_standalone:
			frappe.throw(f"Standalone is fixed once {self.name} is set up.")

	def on_update(self):
		"""The DNS record follows the flag, so a replica never derives a name nothing resolves. Routes
		need no hook: moving a box between ingresses moves both tables' hashes for the next tick."""
		before = self.get_doc_before_save()
		was_standalone = bool(before and before.is_standalone)
		if self.is_standalone and not was_standalone and self.machine_ip:
			self.sync_dns_records()
		terminated = self.has_value_changed("status") and self.status == "Terminated"
		if was_standalone and (terminated or not self.is_standalone):
			self.remove_dns_records()

	def on_trash(self):
		if self.is_standalone:
			self.remove_dns_records()

	def validate_instance_store(self):
		"""The checkbox is only honest on a box that has the hardware."""
		if not self.use_instance_store_for_hf_cache or not self.machine:
			return
		disks = frappe.db.get_value("Machine", self.machine, "instance_store_disks")
		if not disks:
			frappe.throw(
				f"Machine {self.machine} has no instance-store NVMe (run Sync Instance Type "
				"on it if that looks wrong) — untick Use Instance Store For HF Cache."
			)

	def validate_ingress_network(self):
		"""An ingress can only reach this box privately if the two share a VPC. Checked here
		because nothing downstream can say so: a mismatch produces an ingress with an empty table
		and a model that reads unavailable, days later, with nothing pointing back here."""
		if not self.ingress:
			return
		ingress_network = frappe.db.get_value("Ingress Server", self.ingress, "network")
		box_network = frappe.db.get_value("Machine", self.machine, "network") if self.machine else None
		if ingress_network != box_network:
			frappe.throw(
				f"Ingress Server {self.ingress} is in Network {ingress_network or 'none'}, but "
				f"{self.name} is in {box_network or 'none'}. An ingress reaches only the boxes "
				f"inside its own VPC."
			)

	def before_insert(self):
		super().before_insert()
		self.default_to_network_singletons()

	def before_rename(self, old, new, merge=False):
		if self.is_standalone:
			frappe.throw(
				f"{old} is Standalone: its name is its DNS record and every replica's Engine URL."
			)

	def default_to_network_singletons(self):
		"""A Network with exactly one ingress, or one monitoring agent, leaves no choice to make,
		so a new server takes it. Two or more stay an operator's pick; a Terminated one is not a
		candidate. Reads the Machine's Network live, like validate_ingress_network."""
		network = self.machine and frappe.db.get_value("Machine", self.machine, "network")
		if not network:
			return
		# Membership off the Machine, live: a server's own network field is a mirror as old as
		# its last save, and one that predates the field never matches.
		boxes = frappe.get_all("Machine", filters={"network": network}, pluck="name")
		for field, doctype in (("ingress", "Ingress Server"), ("monitoring_agent", "Monitoring Agent")):
			if self.get(field) or (field == "ingress" and self.is_standalone):
				continue
			names = frappe.get_all(
				doctype,
				filters={"machine": ("in", boxes), "status": ("!=", "Terminated")},
				pluck="name",
			)
			if len(names) == 1:
				self.set(field, names[0])

	@property
	def archive_blockers(self):
		"""A replica that is not Terminated still owns cards, a port and a route on this box."""
		replicas = frappe.get_all(
			"Model Replica",
			filters={"inference_server": self.name, "status": ("!=", "Terminated")},
			pluck="name",
		)
		if not replicas:
			return []
		return [f"Replicas still placed here: {', '.join(replicas)}. Tear them down first."]

	# ── The box ───────────────────────────────────────────────────────────────
	# Everything reaching the hardware goes through here: a Model Replica talks to its Inference
	# Server, and the Server is the only side that knows about a Machine.

	@property
	def hf_home(self):
		"""The instance-store mount when opted in, the data volume otherwise."""
		if self.use_instance_store_for_hf_cache:
			return "/mnt/instance/hf"
		return f"{self.data_path}/hf"

	@property
	def machine_doc(self):
		"""The box this server runs on."""
		if not self.machine:
			frappe.throw(f"Inference Server {self.name} has no Machine to reach.")
		return frappe.get_doc("Machine", self.machine)

	@property
	def gpus(self):
		"""The cards on this box, in CUDA index order. `gpu_type` is the catalogue record every
		source's spelling resolves to, and `vram_gb` is fetched off it."""
		if not self.machine:
			return []
		return cards_on([self.machine])

	def run_command(self, command, timeout=60):
		"""Run one argv on this server's box over SSH."""
		return self.machine_doc.run_command(command, timeout=timeout)

	def stream_command(self, command):
		"""Follow one argv on this server's box, yielding its output line by line."""
		return self.machine_doc.stream_command(command)

	@frappe.whitelist()
	def get_gpu_allocation(self):
		"""The box's GPUs and which replica holds each. One query: the holder is a column on the
		card, so this panel and the placement that refuses a taken card read the same row.

		A blank `held_by` is genuinely free — a stopped replica released its cards on purpose."""
		gpus = self.gpus
		for gpu in gpus:
			gpu.deployments = (
				[frappe.db.get_value("Model Replica", gpu.held_by, ["name", "model"], as_dict=True)]
				if gpu.held_by
				else []
			)
			gpu.status = "Allocated" if gpu.held_by else "Free"
		return gpus

	@property
	def free_gpus(self):
		"""`get_gpu_allocation` read the other way round, and live for the same reason.

		A replica pinning no cards claims none, so it does NOT show here: a box running one looks
		emptier than it is, which is why the scheduler declines such a box."""
		return [gpu for gpu in self.get_gpu_allocation() if gpu.status == "Free"]

	@frappe.whitelist()
	def update_scrape_auth(self):
		"""Button: rewrite this box's metrics htpasswd from the current scrape hash, re-running the
		exporters with it (and DCGM if cards have appeared since Setup). Setup installs all of that
		already; this is the path after a Scrape Password rotation, which no box learns of by itself."""
		if not self.machine:
			frappe.throw("Set a Machine before updating its scrape auth.")
		frappe.enqueue_doc(
			self.doctype, self.name, "provision_exporters", queue="long", timeout=1800
		)
		frappe.msgprint(f"Updating scrape auth on {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=False)
	def provision_exporters(self):
		return run_exporters_play(self)

	@frappe.whitelist()
	def setup(self):
		"""Button: one-time host bootstrap (NVIDIA driver + data volume) via the
		gpu_host role. Run once per box before deploying models onto it — Model
		Deployment.setup gates on is_provisioned."""
		if not self.machine:
			frappe.throw("Set a Machine before provisioning.")
		if not (self.ingress or self.is_standalone):
			frappe.throw(
				"Pick the Ingress Server that fronts this box, or tick Standalone to have the "
				"gateways dial it directly — Setup installs a different front for each."
			)
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"provision",
			queue="long",
			# Has to outlast the driver reboot, which is half an hour on a bare metal box.
			timeout=5400,
		)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		"""One-time host bootstrap for an Inference Server: NVIDIA driver + data
		volume + Docker (the gpu_host role), then its metrics exporters (node_exporter,
		dcgm_exporter if the Machine has GPUs) — all in the one provision.yml play, so Setup
		is a single Ansible run. Runs once per box — model serves (deploy_model) assume an
		already-provisioned host and gate on is_provisioned. Mirrors deploy_agent on the
		proxy side."""
		frappe.db.set_value("Inference Server", self.name, "status", "Installing")
		frappe.db.commit()

		is_bare_metal = frappe.db.get_value("Machine", self.machine, "is_bare_metal")
		play_name, rc = self.run_playbook(
			"provision.yml",
			extravars={
				"gpu_data_mount": self.data_path,
				"gpu_instance_store_hf_cache": bool(self.use_instance_store_for_hf_cache),
				"monitoring_has_gpu": bool(self.gpus),
				# The driver reboot outlasts Ansible's default on a bare metal box.
				"gpu_reboot_timeout": 1800 if is_bare_metal else 600,
				# The engine proxy's htpasswd: this play and serve.yml both write it, from the
				# one source, so whichever runs last cannot disagree with the other.
				**frappe.get_single("Grove Settings").scrape_auth_variables,
				**self.tls_variables,
			},
		)

		ok = rc == 0
		frappe.db.set_value(
			"Inference Server",
			self.name,
			{"status": "Active" if ok else "Broken", "is_provisioned": 1 if ok else 0},
		)
		if ok and self.is_standalone:
			self.sync_dns_records()
		return play_name, rc

	# ── The front ─────────────────────────────────────────────────────────────
	# Standalone: its own nginx on the fleet name and wildcard. Otherwise an ingress fronts it.

	@property
	def front_url(self):
		"""Where the gateways dial this box: its fleet name when standalone and the fleet publishes
		names (what dns_client needs), its public IP otherwise."""
		if self.is_standalone and self.has_fleet_name:
			return f"https://{self.hostname}"
		if not self.machine_ip:
			frappe.throw(
				f"Inference Server {self.name} has no machine IP (set its Machine's public IP) — "
				"nothing to dial."
			)
		return f"https://{self.machine_ip}"

	@property
	def tls_variables(self):
		"""A standalone box's nginx serves the fleet wildcard once one is issued; any other box keeps
		grove_https's self-signed box.crt. Carries the key, so resolve it inside the job."""
		variables = FleetHost.tls_variables.fget(self)
		if not (self.is_standalone and variables["fleet_tls_cert"]):
			return {}
		return {
			**variables,
			# nginx's master reads it as root, and the inference plays create no frappe user.
			"fleet_tls_key_owner": "root",
			# Ansible resolves these against the fleet_tls role, the one owner of the paths.
			"grove_tls_cert": "{{ fleet_tls_cert_path }}",
			"grove_tls_key": "{{ fleet_tls_key_path }}",
			"grove_tls_selfsigned": False,
		}

	@frappe.whitelist()
	def deploy_tls(self):
		"""Button + renewal push: rewrite the fleet certificate and reload nginx onto it."""
		return self.run_playbook("deploy_tls.yml", extravars=self.tls_variables)
