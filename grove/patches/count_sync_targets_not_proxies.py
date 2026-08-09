"""Gateway Sync.proxies_total/ok → targets_total/ok.

A run pushes to two kinds of box now — gateways take keys, users, groups and routes; ingresses take
a replica table — and both have been counted in these two fields since the split. "Proxies Total"
of 2 on a run that reached one gateway and one ingress is a number that reads as a lie.

Int fields on a log doctype, so rename_field carries the data as it stands."""

import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	frappe.reload_doctype("Gateway Sync")
	for old, new in (("proxies_total", "targets_total"), ("proxies_ok", "targets_ok")):
		if frappe.db.has_column("Gateway Sync", old):
			rename_field("Gateway Sync", old, new)
