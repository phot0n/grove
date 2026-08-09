# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""How a server doc names itself: `<prefix><n>-<region>`, e.g. `gw1-ap-south-1`.

Generated rather than typed because the name is not a label — it is infrastructure. A Gateway
Server's and an Ingress Server's name IS their DNS record under the fleet zone, and every server's
name is a part of the request ids it stamps. Left to an operator, those drift into whatever reads
well that afternoon, and the fleet ends up with `gw-ap-south` beside `mc-sg-proxy`.

The region is a suffix, not a namespace, because a name has to be one DNS label: `*.<zone>` covers
`gw1-ap-south-1.<zone>` and nothing deeper. A box with no region — a colo machine that is in no
Network — simply has no suffix, which is exactly what it has to say.
"""

import re

import frappe

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
		self.name = self._chosen_name or next_server_name(
			self.doctype, self.name_prefix, self.machine
		)


def next_server_name(doctype, prefix, machine):
	"""The next free name for a server of this kind on this Machine.

	Numbered per region, so each region starts at 1 and the number stays small enough to say out
	loud. The index is the highest ALREADY USED plus one, never the lowest free one: a name that
	has been retired may still have a DNS record or a log history behind it, and handing it to a
	different box would quietly merge the two.

	Terminated servers still count — their docs are the record that the name was used."""
	suffix = slugify(frappe.db.get_value("Machine", machine, "region") or "") if machine else ""
	stem = f"{prefix}{{}}-{suffix}" if suffix else f"{prefix}{{}}"
	used = re.compile(f"^{re.escape(prefix)}([0-9]+){re.escape('-' + suffix) if suffix else ''}$")
	taken = [
		int(match.group(1))
		for name in frappe.get_all(doctype, pluck="name")
		if (match := used.match(name or ""))
	]
	return stem.format(max(taken, default=0) + 1)
