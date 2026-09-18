# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""One class per placement preference. Lower wins, and a policy composes them in order. Each reads
the Candidate and nothing else — no queries, no doctypes."""

from grove.placement.base import Candidate, Scorer


class WarmCache(Scorer):
	"""Prefer a box that already has this model's weights, so the play skips the download. The
	Candidate decides whether that is true — including that it is worth nothing for a model
	streamed from S3, where no box is warmer than another."""

	def score(self, candidate: Candidate) -> float:
		return 0 if candidate.has_local_weights else 1


class SpreadRegions(Scorer):
	"""Prefer a region this deployment is thin in. Replicas all landing in one region die with
	it."""

	def score(self, candidate: Candidate) -> float:
		return candidate.replicas_in_region


class BestFit(Scorer):
	"""Prefer the box with the least left over, so whole boxes stay free for shapes that need
	them. Over several replicas this CONSOLIDATES: the partly-used box wins until it is full."""

	def score(self, candidate: Candidate) -> float:
		return candidate.surplus


class WorstFit(Scorer):
	"""Prefer the emptiest box. BestFit's inverse, and it DISTRIBUTES: taking cards off the
	emptiest box makes another box the emptiest, so successive replicas land elsewhere."""

	def score(self, candidate: Candidate) -> float:
		return -candidate.surplus


class FewestReplicas(Scorer):
	"""Prefer the box running the least. A tie-break, not a load model — what a replica actually
	costs a box is its cards, and those are already spent by the time this is asked."""

	def score(self, candidate: Candidate) -> float:
		return candidate.active_replicas
