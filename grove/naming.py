# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""How a server doc names itself: `<prefix><n>-<region>`, e.g. `gw1-ap-south-1`.

Generated rather than typed because the name is not a label — it is infrastructure. A Gateway
Server's and an Ingress Server's name IS its DNS record under the fleet zone, and every server's
name is part of the request ids it stamps. Left to an operator those drift into whatever reads well
that afternoon, and the fleet ends up with `gw-ap-south` beside `mc-sg-proxy`.

The number comes from `tabSeries`, Frappe's own counter, under a key that includes the region — so
each region counts from 1 and two inserts racing each other cannot land on the same number, which a
read-then-add-one would. The name is then assembled here rather than by a naming series, because a
series keys its counter on whatever precedes the `#`: put the digits after the prefix, as
`gw1-ap-south-1` does, and the key is `gw` and every region shares one counter. Position and scope
are the same decision there, and this is the only way to have both.

The region is a suffix, not a namespace, because a name has to be one DNS label: `*.<zone>` covers
`gw1-ap-south-1.<zone>` and nothing deeper. A box with no region — a colo machine in no Network —
simply has no suffix, which is exactly what it has to say.
"""

import frappe
from frappe.model.naming import getseries

from grove.utils import slugify


class GeneratedName:
	"""Mixin: name a server `<name_prefix><n>-<region>` unless the caller chose one.

	Frappe blanks `doc.name` before calling `autoname()` — that is what stops a client inventing
	its own name — so a name the caller DID mean is caught in `before_naming`, which runs first,
	and put back. Without that, `frappe.get_doc({..., "name": "x"}).insert()` would silently get a
	generated name instead, which is a footgun for fixtures and one-off scripts."""

	name_prefix = ""

	def before_naming(self):
		self._chosen_name = self.name

	def autoname(self):
		self.name = self._chosen_name or next_server_name(self.name_prefix, self.machine)


def next_server_name(prefix, machine, counter=None):
	"""The next name for a server of this kind on this Machine.

	`counter` is the number source, defaulting to Frappe's `tabSeries`. Injectable for the same
	reason `parse_naming_series` takes one: the real counter is a row in a table that no test
	rollback undoes, so a test that wanted to assert a number could never assert the same one twice.

	The series only ever climbs, and never reuses a number even when the doc that took it is
	deleted. That is the behaviour worth having: a retired name may still have a DNS record or a
	log history behind it, and handing it to a different box would quietly merge the two."""
	counter = counter or getseries
	region = slugify(frappe.db.get_value("Machine", machine, "region") or "") if machine else ""
	# The series key, not the name — it ends in '-' the way Frappe's own keys do, and it is what
	# makes the count per region.
	number = counter(f"{prefix}-{region}-" if region else f"{prefix}-", 1)
	return f"{prefix}{number}-{region}" if region else f"{prefix}{number}"
