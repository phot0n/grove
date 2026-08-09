"""Gateway Sync.proxies_total/ok → targets_total/ok.

A run pushes to two kinds of box now — gateways take keys, users, groups and routes; ingresses take
a replica table — and both have been counted in these two fields since the split. "Proxies Total"
of 2 on a run that reached one gateway and one ingress is a number that reads as a lie.

Int fields on a log doctype, so rename_field carries the data as it stands."""

import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	# Resolved, not hardcoded: the doctype is renamed to Agent Sync by a pre-model-sync patch,
	# which runs before this one on a site that has not migrated yet.
	doctype = "Agent Sync" if frappe.db.exists("DocType", "Agent Sync") else "Gateway Sync"
	frappe.reload_doctype(doctype)
	for old, new in (("proxies_total", "targets_total"), ("proxies_ok", "targets_ok")):
		if frappe.db.has_column(doctype, old):
			rename_field(doctype, old, new)
