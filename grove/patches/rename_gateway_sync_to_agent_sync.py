"""Gateway Sync → Agent Sync, and its child table with it.

The doc logs a push to a box running the grove-gateway agent, and since the split that box is a
gateway OR an ingress. "Gateway Sync" named half of what it records — the same way "Proxy Server"
named the wrong thing once an ingress existed.

Pre-model-sync, so the tables move before the JSON that now describes them is applied.

Also drops the Scheduled Job Type row pointing at the old module path. grove/gateway_sync.py is
grove/agent_sync.py now, and a Scheduled Job Type stores its method as a string: left alone, the
scheduler keeps calling a function that no longer exists and the two-minute repair pass dies every
tick with an ImportError in a log nobody reads. Deleting it is enough — migrate recreates it from
hooks with the new path."""

import frappe


def execute():
	for old, new in (("Gateway Sync Row", "Agent Sync Row"), ("Gateway Sync", "Agent Sync")):
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
	frappe.db.delete("Scheduled Job Type", {"method": ("like", "%grove.gateway_sync.%")})
