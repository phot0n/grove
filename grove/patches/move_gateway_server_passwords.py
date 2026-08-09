"""Move the renamed doctype's stored passwords: __Auth still says `Proxy Server`.

`frappe.rename_doc("DocType", "Proxy Server", "Gateway Server")` moves the table, the Link data and
the doc rows, but NOT the `__Auth` table, which files every Password field under (doctype, name,
fieldname). So `get_password("admin_token")` on a Gateway Server looked for a row that says
`Gateway Server` and found nothing — while the value sat there under the old name.

That is not cosmetic. admin_token is the only credential the control plane has for a gateway, so
every gateway_sync push and every usage_pull drain threw "Password not found" from the moment the
rename landed. Nothing retries a config error, so keys, routes and usage all stopped moving and the
only sign was a Gateway Sync row with an error on it.

UPDATE IGNORE rather than UPDATE: (doctype, name, fieldname) is unique, and a token re-entered by
hand after the rename would already occupy the destination row. The newer value wins and the
orphan is dropped, which is the right way round — the hand-entered one is what the box was last
given."""

import frappe


def execute():
	frappe.db.sql("UPDATE IGNORE `__Auth` SET doctype = 'Gateway Server' WHERE doctype = 'Proxy Server'")
	frappe.db.sql("DELETE FROM `__Auth` WHERE doctype = 'Proxy Server'")
