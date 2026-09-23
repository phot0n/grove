# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Which box a new replica goes on. One Scorer per preference, composed in order by a named policy,
and `placement_policy` is the one place that dispatch happens.

Nothing here imports frappe, so every test in this package is pure: no site, no mocking.

Scoring only ORDERS boxes that are already viable. What makes a box viable is decided by the caller
and is deliberately not pluggable: a policy that could skip the architecture check would produce a
container Docker pulls happily and fails at exec, deep inside a play, with nothing naming why."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class PlacementError(Exception):
	pass


@dataclass(frozen=True)
class Candidate:
	"""One box, already measured against ONE deployment's shape — which is why a Scorer needs
	nothing but this."""

	inference_server: str
	region: str = ""
	# Free cards meeting the deployment's filters, as GPU docnames, lowest index first. Names, not
	# CUDA indices: a MIG slice has no index, and a number resolved against the box later may have
	# been renumbered onto other silicon by a scan.
	fitting_gpus: tuple = ()
	# Spare cards left over if this replica lands here. Negative means it does not fit.
	surplus: int = 0
	# A sibling replica of this Model is here AND the weights are not streamed from S3. The caller
	# decides both halves.
	has_local_weights: bool = False
	active_replicas: int = 0
	replicas_in_region: int = 0
	rejection: str = ""

	@property
	def is_viable(self):
		return not self.rejection


class Scorer(ABC):
	"""One placement preference. Lower wins. Stateless: one instance is shared by every deployment
	using a policy that names it."""

	@abstractmethod
	def score(self, candidate: Candidate) -> float: ...


def sort_key(candidate, scorers):
	"""A tuple, so the ORDER of a policy's scorers is its tiebreak order — the second scorer only
	speaks where the first ties."""
	return tuple(scorer.score(candidate) for scorer in scorers)


def placement_policy(policy):
	"""The scorers a `placement_policy` runs, in order. Add a policy by adding one entry here;
	`find_placement` never changes.

	An unknown key raises rather than defaulting: a policy string that quietly became `balanced`
	would place replicas somewhere nobody asked for and look like it worked."""
	from grove.placement.scorers import BestFit, FewestReplicas, SpreadRegions, WarmCache, WorstFit

	policies = {
		"balanced": (WarmCache(), SpreadRegions(), BestFit(), FewestReplicas()),
		# Region is not weighed at all, so replicas gather onto as few boxes as the shape allows.
		"pack": (WarmCache(), BestFit(), FewestReplicas()),
		# Warmth only breaks a remaining tie, so one box taking everything is the last thing.
		"spread": (SpreadRegions(), WorstFit(), WarmCache(), FewestReplicas()),
	}
	scorers = policies.get(policy)
	if not scorers:
		raise PlacementError(f"No placement policy '{policy}'.")
	return scorers


def fitting_gpus(free_gpus, gpu_type="", min_vram_gb=0):
	"""Which of a box's free cards meet a deployment's hard filters, as GPU docnames. `free_gpus`
	is rows carrying `name`, `gpu_type` and `vram_gb`.

	Returned in the order they were GIVEN: `cards_on` orders by CUDA index, which is what "take the
	first N" should mean, and sorting docnames would order by a hash instead.

	`gpu_type` is matched for EQUALITY, which only works because both sides now resolve to one
	`GPU Type` — it was a substring test back when nvidia-smi said `Tesla T4` and AWS said `T4`."""
	wanted = (gpu_type or "").strip()
	matches = []
	for gpu in free_gpus:
		if wanted and (gpu.get("gpu_type") or "") != wanted:
			continue
		if min_vram_gb and (gpu.get("vram_gb") or 0) < min_vram_gb:
			continue
		matches.append(gpu["name"])
	return tuple(matches)
