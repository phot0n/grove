"""Which address one Grove box reaches another on.

A Network is one VPC, one subnet, one availability zone, so two boxes that share one have a private
route between them and nothing else does. That one rule decides which address a scrape targets and
which addresses a security group opens, so it lives here rather than inside either of them —
a firewall should not have to import from a metrics module to know how boxes reach each other.
"""


from urllib.parse import urlparse


def reachable_ip(target, viewer_network):
	"""The target's private address when it shares the viewer's Network, its public one otherwise.

	`target` is a plain dict carrying `network`, `private_ip` and `ip` — the shape the monitoring
	queries already select, with the public address aliased to `ip`.

	Public is the answer for a pod (no Machine and so no Network at all), for a colo or bare-metal
	box with no private address, and for a viewer that is itself in no Network."""
	if viewer_network and target.get("network") == viewer_network and target.get("private_ip"):
		return target["private_ip"]
	return target.get("ip")


def private_url(engine_url, private_ip):
	"""`engine_url` with its host swapped for `private_ip`. "" when either is missing or the URL
	has no host to swap.

	Deliberately not reachable_ip: that one answers "the best address available" and falls back to
	public, which is right for a scrape and wrong for an ingress. An ingress never CHOOSES an
	address — it is inside the VPC by construction, so the private one is the only address it has,
	and a replica without one is left out of its table rather than dialled over the internet.

	Only the host moves. The box's front is on 443 today and moves to 80 with the cutover, so the
	scheme has to come from the URL the control plane already built rather than be assumed here —
	one rule that survives the move instead of two that disagree during it. A port, if the URL
	carries one, is the engine's and stays."""
	if not (engine_url and private_ip):
		return ""
	parsed = urlparse(engine_url)
	if not parsed.hostname:
		return ""
	host = f"{private_ip}:{parsed.port}" if parsed.port else private_ip
	return parsed._replace(netloc=host).geturl()
