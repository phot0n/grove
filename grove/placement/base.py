# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Which box a new replica goes on. One Scorer per preference, composed in order by a named
policy, and `placement_policy` is the one place that dispatch happens — `Model Deployment` never
names a concrete Scorer or branches on the policy string.

Nothing here imports frappe. A Candidate is measured against one deployment's shape before it
arrives, so every test in this package is pure: no site, no mocking.

Scoring only ORDERS boxes that are already viable. What makes a box viable — architecture, free
cards, the deployment's hard filters — is decided by the caller and is deliberately not pluggable:
a policy that could skip the architecture check would produce a container Docker pulls happily and
fails at exec, deep inside a play, with nothing naming the cause."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class PlacementError(Exception):
	pass


@dataclass(frozen=True)
class Candidate:
	"""One box, already measured against ONE deployment's shape — which is why a Scorer needs
	nothing but this. `rejection` is why the box cannot take the replica, or None."""

	inference_server: str
	region: str = ""
	# Free cards meeting the deployment's gpu_type / min_vram_gb, as GPU docnames, lowest
	# index first. Names rather than CUDA indices because a MIG slice has no index, and because
	# a number has to be resolved against the box again later — by which time a scan may have
	# renumbered it onto other silicon.
	fitting_gpus: tuple = ()
	# Spare cards left over if this replica lands here. Negative means it does not fit.
	surplus: int = 0
	# A sibling replica of the same Model is already on this box AND the weights are not streamed
	# from S3 — so the HF cache here is worth something. The caller decides both halves.
	has_local_weights: bool = False
	active_replicas: int = 0
	replicas_in_region: int = 0
	rejection: str = ""

	@property
	def is_viable(self):
		return not self.rejection


class Scorer(ABC):
	"""One placement preference. Lower wins.

	Stateless: one instance is shared by every deployment using a policy that names it. A Scorer
	that needed per-call state would be reading something a Candidate should already carry."""

	@abstractmethod
	def score(self, candidate: Candidate) -> float: ...


def sort_key(candidate, scorers):
	"""What the winner is chosen by. A tuple, so the ORDER of a policy's scorers is its tiebreak
	order — the second scorer only speaks where the first ties."""
	return tuple(scorer.score(candidate) for scorer in scorers)


def placement_policy(policy):
	"""The scorers a Model Deployment's `placement_policy` runs, in order. Add a policy by adding
	one entry here; add a preference by adding one Scorer. `find_placement` never changes.

	An unknown key raises rather than falling back to a default: a policy string that quietly
	became `balanced` would place replicas somewhere nobody asked for and look like it worked."""
	from grove.placement.scorers import BestFit, FewestReplicas, SpreadRegions, WarmCache, WorstFit

	policies = {
		# The default: a warm box, then a region this deployment is thin in, then the tightest
		# fit so a whole box is left for a shape that needs one, then the quietest box.
		"balanced": (WarmCache(), SpreadRegions(), BestFit(), FewestReplicas()),
		# Consolidate. Tightest fit outright and region is not weighed at all, so replicas gather
		# onto as few boxes as the shape allows — BestFit, because best fit is the packing one.
		"pack": (WarmCache(), BestFit(), FewestReplicas()),
		# Distribute. Thinnest region, then the emptiest box, so one region or one box taking
		# everything is the last thing that happens; warmth only breaks a remaining tie.
		"spread": (SpreadRegions(), WorstFit(), WarmCache(), FewestReplicas()),
	}
	scorers = policies.get(policy)
	if not scorers:
		raise PlacementError(f"No placement policy '{policy}'.")
	return scorers


def fitting_gpus(free_gpus, gpu_type="", min_vram_gb=0):
	"""Which of a box's free cards meet a deployment's hard filters, as GPU docnames.

	`free_gpus` is rows carrying `name`, `gpu_type` and `vram_gb`.

	Returned in the order they were GIVEN, not sorted: `cards_on` orders by CUDA index, which is
	what "take the first N" should mean, and sorting docnames would order by a hash instead.

	`gpu_type` is matched for EQUALITY. It used to be a case-insensitive substring test on a free
	text model name, because both sides were unvalidated — nvidia-smi said `Tesla T4` where AWS
	said `T4`, so equality matched nothing. Both now resolve to one `GPU Type`, so the guess is
	gone: a deployment asking for `T4` matches a T4 whichever source found it."""
	wanted = (gpu_type or "").strip()
	matches = []
	for gpu in free_gpus:
		if wanted and (gpu.get("gpu_type") or "") != wanted:
			continue
		if min_vram_gb and (gpu.get("vram_gb") or 0) < min_vram_gb:
			continue
		matches.append(gpu["name"])
	return tuple(matches)
