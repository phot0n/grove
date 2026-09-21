# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""How a Machine names itself (`gw1-ap-south-1`), how the server doc on it takes that name, and how
a Model Replica names itself off those parts (`qwen3-8b-ap-south-1-inf3-00007`).

Generated rather than typed because the name is infrastructure: a Gateway or Ingress Server's name
IS its DNS record under the fleet zone, and every server's name is part of the request ids it
stamps. Named once, on the Machine, so a box and the server on it never answer to two names.

The number comes from `tabSeries` under a key that includes the region, so each region counts from
1 and two racing inserts cannot land on the same number. The name is then assembled HERE rather
than by a naming series, because a series keys its counter on whatever precedes the `#` — put the
digits after the prefix and every region shares one counter. Position and scope are the same
decision there, and this is the only way to have both.

The region is a suffix, not a namespace, because the first label is all `*.<zone>` covers. A box in
a Geography with a zone is named for its whole hostname, `gw1-ap-south-1.<zone>`, so the domain shows
at a glance; request ids and the agent carry only `short_name`. A box with no region simply has no
region suffix, and one with no zone no domain.
"""

from frappe.model.naming import getseries

from grove.utils import slugify


class GeneratedName:
	"""Mixin: name a doc `get_generated_name()` unless the caller chose one.

	Frappe blanks `doc.name` before `autoname()`, which is what stops a client inventing its own —
	so a name the caller DID mean is caught in `before_naming` and put back. Without that,
	`get_doc({..., "name": "x"}).insert()` silently gets a generated one."""

	def before_naming(self):
		self._chosen_name = self.name

	def autoname(self):
		self.name = self._chosen_name or self.get_generated_name()


def next_machine_name(prefix, region, zone="", counter=None):
	"""The next `<prefix><n>-<region>[.<zone>]` for a box of this kind in this region.

	`counter` is injectable because the real one is a row no test rollback undoes, so a test could
	never assert the same number twice.

	The series only ever climbs and never reuses a number, even when the doc that took it is
	deleted: a retired name may still have a DNS record or a log history behind it."""
	counter = counter or getseries
	region = slugify(region)
	# The series key, not the name — it is what makes the count per region.
	number = counter(f"{prefix}-{region}-" if region else f"{prefix}-", 1)
	name = f"{prefix}{number}-{region}" if region else f"{prefix}{number}"
	return f"{name}.{zone}" if zone else name


def short_name(name):
	"""`gw1-ap-south-1.<zone>` → `gw1-ap-south-1`: the one label a box is known by inside its zone,
	and what a request id can carry. A name with no domain is already short."""
	return (name or "").partition(".")[0]


# One counter behind a deployment's name and the number on the end of a replica's, and the key
# the old `MD-{#####}` format used — so the numbers carry on climbing.
#
# Sharing is not what keeps `MD-` names unique: legacy replicas were named by an OLD naming_series
# whose key is not this one. What keeps them apart is the floor the rename patch put under this
# counter. Two doctypes are two tables, so a clash never fails an insert — the name just quietly
# means both, and a route row and an access log both carry `deployment=<name>`.
MD_SERIES = "MD-"


def abbreviate_server(server):
	"""`inf3-ap-south-1` → `inf3`. The replica name carries the region already."""
	return slugify(server).split("-")[0]


def next_replica_name(model, server, region, counter=None):
	"""A replica's name: `<model id>-<region>-<server>-<n>`, e.g.
	`qwen3-8b-ap-south-1-inf3-00007`.

	Says what it serves, where, and off which box, without opening the doc. The region goes in as
	the Region doc names it — every provider codes its regions differently (`ap-south-1`,
	`asia-south1`, `southeastasia`), and a shortening rule fitted to one of them mangles the rest.
	The number is what keeps the name unique — two replicas of one model on one box are a
	legitimate thing to have — and comes from `tabSeries` for the same reason server names do: two
	inserts racing cannot land on it twice. `counter` is injectable, as `next_machine_name`'s is."""
	counter = counter or getseries
	parts = [slugify((model or "").split("/")[-1]), slugify(region), abbreviate_server(server)]
	return "-".join([part for part in parts if part] + [counter(MD_SERIES, 5)])


def next_deployment_name(counter=None):
	"""A deployment's name: `MD-{#####}`, e.g. `MD-00014`.

	Numbered out of `tabSeries` rather than by `append_number_if_name_exists`, because several
	deployments of one model are a normal thing to have — a second shape, or a rollout running the
	old one and the new one side by side — and that helper reads the taken names and then picks
	the next, which two inserts racing can both do before either writes.

	Neither the model nor the shape is in the name. `gpus_per_replica` is editable and a name is
	not, so `4xh100` would go stale the first time someone re-shaped this; the model is one field
	away and the list view carries both, where they stay true."""
	return f"{MD_SERIES}{(counter or getseries)(MD_SERIES, 5)}"
