"""Agent Sync → Pathway Sync, and its child table with it.

The agent this doc logs a push to is called pathway now — the binary, the systemd unit, the module
and the paths all moved. "Agent" was only ever a description of the thing on the far end, so it
stopped naming anything once the thing had a name of its own.

Third link in the chain: Gateway Sync → Agent Sync → Pathway Sync. The two post-model-sync patches
that touch these tables resolve the name newest-first for exactly this reason, so they keep working
whichever link a site is on.

Pre-model-sync, so the tables move before the JSON that now describes them is applied.

Also drops the Scheduled Job Type row pointing at the old module path. grove/agent_sync.py is
grove/pathway_sync.py now, and a Scheduled Job Type stores its method as a string: left alone, the
scheduler keeps calling a function that no longer exists and the two-minute projection push dies
every tick with an ImportError in a log nobody reads. Deleting it is enough — migrate recreates it
from hooks with the new path.
"""

import frappe


def execute():
	for old, new in (("Agent Sync Row", "Pathway Sync Row"), ("Agent Sync", "Pathway Sync")):
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
	frappe.db.delete("Scheduled Job Type", {"method": ("like", "%grove.agent_sync.%")})
