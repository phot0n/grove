# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
import requests
from frappe.model.document import Document

from grove.ansible import Ansible
from grove.utils import ansible_project_dir


class MonitoringAgent(Document):
	"""One vmagent, on its own Machine, scraping every box and pod that names it.

	It discovers what to scrape by asking Grove (grove.monitoring.targets), so a deploy, a
	new box or a terminated pod changes the target list without touching this agent."""

	@frappe.whitelist()
	def setup(self):
		"""Button: install vmagent on this agent's Machine (long job — it SSHes to the box)."""
		self.preflight()
		frappe.enqueue_doc(self.doctype, self.name, "provision", queue="long", timeout=1800)
		frappe.msgprint(f"Installing vmagent on {self.name} — watch its Ansible Plays.")

	def preflight(self):
		"""Everything agent.yml needs, checked before it is queued.

		The play installs node_exporter before vmagent, so a setting missing from Grove Settings
		used to leave a half-built box behind and a failed play to read it out of. Collects every
		problem rather than throwing on the first — one trip through the form beats finding them
		one failed install at a time."""
		variables = frappe.get_single("Grove Settings").monitoring_variables
		problems = []

		if not self.machine:
			problems.append("Set a Machine — this agent has no box to install on.")
		elif not frappe.db.get_value("Machine", self.machine, "public_ip"):
			problems.append(f"Machine {self.machine} has no public IP — nothing to SSH to.")

		if not variables["monitoring_remote_write_url"]:
			problems.append("Grove Settings → Metrics Remote Write URL is empty — nowhere to push.")
		if not variables["monitoring_remote_write_token"]:
			problems.append("Grove Settings → Metrics Token is empty — the ingestion service would reject the push.")

		problems += self.get_target_list_problems(variables)

		if problems:
			frappe.throw("<br>".join(problems), title=f"{self.name} is not ready to install")

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

	def provision(self):
		"""Job: agent.yml with where to push (Grove Settings) and which target list to pull
		(this agent's own name). Mirrors InferenceServer.provision."""
		settings = frappe.get_single("Grove Settings")
		frappe.db.set_value(self.doctype, self.name, "status", "Installing")
		frappe.db.commit()

		ansible = Ansible(project_root=ansible_project_dir("monitoring"))
		play_name, rc = ansible.run_playbook(
			playbook_name="agent.yml",
			server_type="Monitoring Agent",
			server_name=self.name,
			machine_name=self.machine,
			extravars={
				**settings.monitoring_variables,
				"monitoring_agent": self.name,
				"monitoring_scrape_interval": self.scrape_interval or "15s",
				"monitoring_engine_scrape_interval": self.engine_scrape_interval or "5s",
				"vmagent_max_disk_usage": self.max_disk_usage or "4GiB",
			},
		)
		frappe.db.set_value(self.doctype, self.name, "status", "Active" if rc == 0 else "Broken")
		return play_name, rc
