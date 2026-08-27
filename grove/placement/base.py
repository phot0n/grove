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
	# Free cards meeting the deployment's gpu_model / min_vram_gb, lowest index first.
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
		# The default: reuse a warm box, then spread across regions so a deployment does not die
		# with one, then leave the biggest boxes whole.
		"balanced": (WarmCache(), SpreadRegions(), BestFit(), FewestReplicas()),
		# Fill a box before opening another. For batch work, where a stranded card costs more
		# than a shared one.
		"pack": (WarmCache(), WorstFit(), FewestReplicas()),
		# Availability first: spread before anything else, even off a warm box.
		"spread": (SpreadRegions(), WarmCache(), BestFit()),
	}
	scorers = policies.get(policy)
	if not scorers:
		raise PlacementError(f"No placement policy '{policy}'.")
	return scorers


def fitting_gpus(free_gpus, gpu_model="", min_vram_gb=0):
	"""Which of a box's free cards meet a deployment's hard filters, lowest index first.

	`free_gpus` is rows carrying `gpu_index`, `gpu_model` and `vram_gb`.

	`gpu_model` is matched case-insensitively on substring, not equality. Both sides are free text
	with no validation — a Machine GPU says `NVIDIA H100 80GB HBM3` and an operator types `h100` —
	so equality would silently match nothing and reject the whole fleet."""
	wanted = (gpu_model or "").strip().lower()
	matches = []
	for gpu in free_gpus:
		if wanted and wanted not in (gpu.get("gpu_model") or "").lower():
			continue
		if min_vram_gb and (gpu.get("vram_gb") or 0) < min_vram_gb:
			continue
		matches.append(int(gpu["gpu_index"]))
	return tuple(sorted(matches))
