# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""How a server doc names itself (`<prefix><n>-<region>`, e.g. `gw1-ap-south-1`) and how a Model
Deployment names itself off those parts (`qwen3-8b-ap-south-1-inf3-00007`).

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


# The counter behind every deployment name, and the key the old `MD-{#####}` format used — so the
# numbers carry on climbing rather than restarting on top of names already taken.
DEPLOYMENT_SERIES = "MD-"


def abbreviate_server(server):
	"""`inf3-ap-south-1` → `inf3`. A server name already carries its region as a suffix, and a
	deployment name carries the region of its own."""
	return slugify(server).split("-")[0]


def next_deployment_name(model, server, region, counter=None):
	"""A deployment's name: `<model id>-<region>-<server>-<n>`, e.g.
	`qwen3-8b-ap-south-1-inf3-00007`.

	Says what it serves, where, and off which box, without opening the doc. The region goes in as
	the Region doc names it — every provider codes its regions differently (`ap-south-1`,
	`asia-south1`, `southeastasia`), and a shortening rule fitted to one of them mangles the rest.
	The number is what keeps the name unique — two deployments of one model on one box are a
	legitimate thing to have — and comes from `tabSeries` for the same reason server names do: two
	inserts racing cannot land on it twice. `counter` is injectable, as `next_server_name`'s is."""
	counter = counter or getseries
	parts = [slugify((model or "").split("/")[-1]), slugify(region), abbreviate_server(server)]
	return "-".join([part for part in parts if part] + [counter(DEPLOYMENT_SERIES, 5)])
