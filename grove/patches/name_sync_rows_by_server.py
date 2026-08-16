"""Gateway Sync Row.proxy → (server_type, server).

A sync row used to name a Gateway Server, because that was the only thing a push could go to. An
ingress takes one too — a different push, to a different plane — so the row names its target the
way Ansible Play already does, with the doctype beside the name.

Every existing row was a gateway by construction. Raw SQL rather than rename_field plus a loop: it
is one UPDATE over a log table, and the column has to be read and written in the same statement."""

import frappe


def execute():
	# Later pre-model-sync patches rename this doctype, and therefore run BEFORE this one on any
	# site that has not migrated yet. Resolved rather than hardcoded, or this silently finds no
	# column and skips the backfill it exists to do. Newest name first — the chain is
	# Gateway Sync Row → Agent Sync Row → Pathway Sync Row.
	doctype = next(
		(
			name
			for name in ("Pathway Sync Row", "Agent Sync Row", "Gateway Sync Row")
			if frappe.db.exists("DocType", name)
		),
		"Pathway Sync Row",
	)
	if not frappe.db.has_column(doctype, "proxy"):
		return
	frappe.db.sql(f"""
		UPDATE `tab{doctype}`
		SET server = proxy, server_type = 'Gateway Server'
		WHERE proxy IS NOT NULL AND proxy != ''
	""")
