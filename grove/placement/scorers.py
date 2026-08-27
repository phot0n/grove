# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""One class per placement preference. Lower wins, and a policy composes them in order.

Each reads the Candidate and nothing else — no queries, no doctypes — which is what keeps a
preference small enough to be obviously right and testable without a site."""

from grove.placement.base import Candidate, Scorer


class WarmCache(Scorer):
	"""Prefer a box that already has this model's weights.

	A sibling replica of the same Model means the HF cache here is already filled, so the play
	skips the download. The Candidate decides whether that is true — including that it is worth
	nothing for a model streamed from S3, where no box is warmer than another."""

	def score(self, candidate: Candidate) -> float:
		return 0 if candidate.has_local_weights else 1


class SpreadRegions(Scorer):
	"""Prefer a region this deployment is thin in.

	Replicas all landing in one region die with it. This is the availability argument a
	region-scoped deployment used to make by fiat, made by preference instead."""

	def score(self, candidate: Candidate) -> float:
		return candidate.replicas_in_region


class BestFit(Scorer):
	"""Prefer the box with the least left over — so an 8-card box is not stranded by a 2-card
	replica that a 2-card box would have taken."""

	def score(self, candidate: Candidate) -> float:
		return candidate.surplus


class WorstFit(Scorer):
	"""Prefer the box with the most left over — fill one box before opening another.

	BestFit's inverse, and the reason `pack` exists: for batch work a stranded card costs more
	than a shared one, which is the opposite of the latency-critical case."""

	def score(self, candidate: Candidate) -> float:
		return -candidate.surplus


class FewestReplicas(Scorer):
	"""Prefer the box running the least. A tie-break, not a load model — what a replica actually
	costs a box is its cards, and those are already spent by the time this is asked."""

	def score(self, candidate: Candidate) -> float:
		return candidate.active_replicas
