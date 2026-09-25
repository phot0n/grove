# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
import requests
from frappe.model.document import Document

from grove import failure
from grove.monitoring import engine_targets, host_targets
from grove.server import Server


class MonitoringAgent(Server, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		engine_scrape_interval: DF.Data | None
		flush_interval: DF.Data | None
		frappe_public_key: DF.Code | None
		geography: DF.Link | None
		machine: DF.Link
		max_disk_usage: DF.Data | None
		metrics_token: DF.Password | None
		network: DF.Link | None
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		region: DF.Link | None
		remote_write_url: DF.Data | None
		scrape_interval: DF.Data | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	"""One vmagent, on its own Machine, scraping every box and pod that names it. It discovers
	what to scrape by asking Grove, so a deploy or a terminated pod changes the target list
	without touching this agent.

	A developer_mode site inverts that and Grove PUSHES the lists: monitoring_sd_url is whatever
	get_url() says, which on a dev site is a laptop the box has no route to."""

	@property
	def archive_blockers(self):
		"""Whatever this agent scrapes would go dark, engines on Pods included."""
		blockers = []
		for doctype in ("Inference Server", "Ingress Server", "Gateway Server", "Pod"):
			names = frappe.get_all(
				doctype,
				filters={"monitoring_agent": self.name, "status": ("!=", "Terminated")},
				pluck="name",
			)
			if names:
				blockers.append(f"{doctype}s still scraped by this agent: {', '.join(names)}.")
		return blockers

	@frappe.whitelist()
	def setup(self):
		"""Button: install vmagent on this agent's Machine."""
		self.preflight()
		frappe.enqueue_doc(self.doctype, self.name, "provision", queue="long", timeout=1800)
		frappe.msgprint(f"Installing vmagent on {self.name} — watch its Ansible Plays.", alert=True)

	@property
	def remote_write_url(self):
		"""Where this agent pushes: its Region's own endpoint, else the fleet-wide one in Grove
		Settings. Resolved on every read; the column of the same name is what the last install
		wrote, for the form."""
		regional = frappe.db.get_value("Region", self.region, "remote_write_url") if self.region else ""
		return regional or frappe.get_single("Grove Settings").metrics_remote_write_url or ""

	@property
	def ansible_variables(self):
		"""Everything the vmagent role reads. One assembly for every play, so no play can render a
		template from a variable this doc never passed it."""
		tuning = {
			"monitoring_scrape_interval": self.scrape_interval,
			"monitoring_engine_scrape_interval": self.engine_scrape_interval,
			"vmagent_flush_interval": self.flush_interval,
			"vmagent_max_disk_usage": self.max_disk_usage,
		}
		return {
			**frappe.get_single("Grove Settings").monitoring_variables,
			**self.target_variables,
			"monitoring_remote_write_url": self.remote_write_url,
			"monitoring_remote_write_token": self.get_password(
				"metrics_token", raise_exception=False
			) or "",
			"monitoring_agent": self.name,
			# Omitted when blank, so the role's defaults/main.yml is the one place each default
			# is written: an extra-var beats a role default even when it is "".
			**{key: value for key, value in tuning.items() if value},
		}

	def preflight(self):
		"""Everything agent.yml needs, checked before it is queued — the play installs
		node_exporter before vmagent, so a missing setting used to leave a half-built box behind.
		Collects every problem rather than throwing on the first."""
		variables = frappe.get_single("Grove Settings").monitoring_variables
		problems = []

		if not self.machine:
			problems.append("Set a Machine — this agent has no box to install on.")
		elif not frappe.db.get_value("Machine", self.machine, "public_ip"):
			problems.append(f"Machine {self.machine} has no public IP — nothing to SSH to.")

		if not self.remote_write_url:
			problems.append(
				f"Nowhere to push — set a Remote Write URL on Region {self.region or '(unset)'}, "
				f"or the fleet-wide one in Grove Settings."
			)
		if not self.get_password("metrics_token", raise_exception=False):
			problems.append("Metrics Token is empty — the ingestion service would reject the push.")

		# Every box's /metrics/* is behind basic auth, so without this the agent installs happily
		# and is refused by everything it scrapes — which reads as a fleet that is down.
		if not variables["monitoring_scrape_password"]:
			problems.append(
				"Grove Settings → Metrics Scrape Password is empty — every box would refuse this "
				"agent's host scrapes."
			)

		# Only when the box does the fetching: a pushed list has nothing here that could be wrong,
		# and on a dev site this is exactly the check that cannot pass.
		if not is_pushing_targets():
			problems += self.get_target_list_problems(variables)

		if problems:
			frappe.throw("<br>".join(problems), title=f"{self.name} is not ready to install")

	@property
	def target_variables(self):
		"""What this agent scrapes, as Ansible vars. The same entries grove.monitoring.targets
		serves — file_sd and http_sd read the identical shape — so pushing changes how they
		arrive, never what is in them. Built either way, so there is one path to go wrong."""
		return {
			"monitoring_static_targets": is_pushing_targets(),
			"monitoring_host_targets": host_targets(self.name),
			"monitoring_engine_targets": engine_targets(self.name),
		}

	@frappe.whitelist()
	def push_targets(self):
		"""Button: write this agent's current target lists to its box. Refused off a dev site,
		where the agent re-fetches on its own and writing files its scrape config does not name
		would look like it had worked."""
		if not is_pushing_targets():
			frappe.throw(
				f"{self.name} fetches its own targets over http_sd — there is nothing to push. "
				f"Pushed lists are a developer_mode fallback for a box that cannot reach Grove."
			)
		if not self.machine:
			frappe.throw(f"Set a Machine on {self.name} — this agent has no box to write to.")
		if self.status != "Active":
			frappe.throw(
				f"{self.name} is {self.status} — install vmagent before pushing targets to it."
			)
		frappe.enqueue_doc(self.doctype, self.name, "write_targets", queue="short", timeout=600)
		frappe.msgprint(f"Pushing targets to {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=False)
	def write_targets(self):
		"""Job: push_targets.yml with the lists as they are right now. Leaves status alone: this
		neither installs nor breaks an agent.

		The FULL variables, not just the lists — that play re-renders scrape.yml, and passing the
		lists alone left the intervals to the role defaults."""
		return self.run_playbook("push_targets.yml", extravars=self.ansible_variables)

	@frappe.whitelist()
	def update_config(self):
		"""Button: re-render this agent's vmagent config on its box and restart it — the
		intervals, buffer size, push endpoint, token and target lists, all from this doc as it
		stands now. No reinstall: the binary is already there, and downloading it again to
		change a scrape interval only makes the change slower to land."""
		if self.status != "Active":
			frappe.throw(f"{self.name} is {self.status} — install it before updating its config.")
		frappe.enqueue_doc(self.doctype, self.name, "write_config", queue="short", timeout=900)
		frappe.msgprint(f"Updating vmagent config on {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=False)
	def write_config(self):
		"""Job: config.yml — every vmagent config task, none of the install ones."""
		play_name, rc = self.run_playbook("config.yml", extravars=self.ansible_variables)
		frappe.db.set_value(self.doctype, self.name, {
			"status": "Active" if rc == 0 else "Broken",
			"remote_write_url": self.remote_write_url,
		})
		return play_name, rc

	@frappe.whitelist()
	def rotate_metrics_token(self, token: str):
		"""Button: replace the bearer token this agent pushes with, here and on its box. The two
		move together — the box keeps pushing with whatever file it holds, so a token changed
		only here would silently start being rejected at the ingestion service."""
		token = (token or "").strip()
		if not token:
			frappe.throw("Paste the new token — the ingestion service issues it, Grove does not mint it.")
		self.metrics_token = token
		self.save(ignore_permissions=True)
		frappe.db.commit()
		if self.status != "Active":
			frappe.msgprint(
				f"Token saved. {self.name} is {self.status} — install it to put the token on the box."
			)
			return
		frappe.enqueue_doc(self.doctype, self.name, "write_config", queue="short", timeout=900)
		frappe.msgprint(f"Token rotated — writing it to {self.name}. Watch its Ansible Plays.", alert=True)

	def get_target_list_problems(self, variables):
		"""Fetch this agent's target list the way vmagent will, and report what is wrong with it.

		An empty list is not a problem — an agent no box names yet still scrapes itself once it
		is up. What matters is that the endpoint answers 200 with a JSON array: a 401 means the
		SD token cannot read the fleet, and vmagent would then scrape nothing for as long as
		nobody thought to look.

		Fetched from here, not from the agent's box: it proves the URL and the token, not that
		the box can route to the site. That last hop still shows up as a down http_sd job."""
		if not variables["monitoring_sd_token"]:
			return ["Grove Settings → Service Discovery Token is empty — vmagent could not ask what to scrape."]

		url = variables["monitoring_sd_url"]
		try:
			response = requests.get(
				url,
				params={
					"agent": self.name,
					"kind": "host",
					# In the query string, exactly as the scrape config sends it — an Authorization
					# header would be rejected by validate_auth before the endpoint ran.
					"token": variables["monitoring_sd_token"],
				},
				timeout=10,
			)
		except requests.RequestException as error:
			return [f"Could not reach {url} — vmagent fetches its targets from there. {error}"]

		if response.status_code != 200:
			return [f"{url} answered {response.status_code}, not 200. {response.text[:200]}"]
		try:
			targets = response.json()
		except ValueError:
			return [f"{url} did not answer with JSON — http_sd reads a JSON array."]
		if not isinstance(targets, list):
			return [f"{url} answered with a {type(targets).__name__}, not the JSON array http_sd reads."]
		return []

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		"""Job: agent.yml — install vmagent, then everything write_config would do. Mirrors
		InferenceServer.provision."""
		frappe.db.set_value(self.doctype, self.name, "status", "Installing")
		frappe.db.commit()

		play_name, rc = self.run_playbook("agent.yml", extravars=self.ansible_variables)
		frappe.db.set_value(self.doctype, self.name, {
			"status": "Active" if rc == 0 else "Broken",
			"remote_write_url": self.remote_write_url,
		})
		return play_name, rc


def is_pushing_targets():
	"""Whether Grove writes the target lists to a box instead of serving them for pull.

	developer_mode is the whole condition, and it is the right one by accident of what it
	means: a dev site's get_url() is a laptop, so the http_sd URL baked into every scrape
	config points somewhere no box in the fleet can reach. Nothing to configure — the sites
	where the pull cannot work are exactly the sites where this is on."""
	return bool(frappe.conf.get("developer_mode"))
