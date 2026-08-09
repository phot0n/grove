"""Which address one Grove box reaches another on.

A Network is one VPC, one subnet, one availability zone, so two boxes that share one have a private
route between them and nothing else does. That one rule decides which address a scrape targets and
which addresses a security group opens, so it lives here rather than inside either of them —
a firewall should not have to import from a metrics module to know how boxes reach each other.
"""


def reachable_ip(target, viewer_network):
	"""The target's private address when it shares the viewer's Network, its public one otherwise.

	`target` is a plain dict carrying `network`, `private_ip` and `ip` — the shape the monitoring
	queries already select, with the public address aliased to `ip`.

	Public is the answer for a pod (no Machine and so no Network at all), for a colo or bare-metal
	box with no private address, and for a viewer that is itself in no Network."""
	if viewer_network and target.get("network") == viewer_network and target.get("private_ip"):
		return target["private_ip"]
	return target.get("ip")
