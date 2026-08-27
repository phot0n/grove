# `placement/` — which box a replica goes on

One class per preference, composed in order by a named policy. `Model Deployment` never names a
concrete Scorer or branches on the policy string — `placement_policy` is the one place that
dispatch happens, the same way `engine_class` is for engines.

**Nothing here imports frappe.** A `Candidate` arrives already measured against one deployment's
shape, so every test in this package is pure: no site, no mocking, no fixtures.

| File | What it is |
|---|---|
| `base.py` | `Candidate`, the `Scorer` contract, `sort_key`, and the `placement_policy` registry. |
| `scorers.py` | Every preference. One class, one `score`, a few lines each. |

## Adding a preference

One class in `scorers.py`, and name it in whichever policies want it. Lower wins. It may read
nothing but the `Candidate` — a Scorer that needed a query is reading something the caller should
have measured and put on the Candidate instead, where it is measured once for the whole fleet
rather than once per box.

## Adding a policy

One entry in `placement_policy`'s dict. The policy string is the `Model Deployment.placement_policy`
Select option, verbatim and lowercase. `find_placement` does not change.

## Scoring orders; it does not admit

A policy ranks boxes that can **already** take the replica. What makes a box viable — the engine
image's architecture, enough free cards, the deployment's `gpu_model` and `min_vram_gb`, and the
engine's own `placement_errors` — is decided in `ModelDeployment._rejection` and is deliberately
not pluggable.

That split is the safety property. A "policy" that could skip the architecture check would produce
a container Docker pulls happily and then fails at exec, deep inside a play, with nothing in the
output naming the cause — which is exactly what `_validate_engine_architecture` exists to stop. A
policy can only ever choose badly among boxes that would all have worked.

## The three policies

| Policy | Order | For |
|---|---|---|
| `balanced` | warm box → thinnest region → tightest fit → fewest replicas | the default |
| `pack` | warm box → tightest fit → fewest replicas | batch work; region is not weighed at all, so replicas gather |
| `spread` | thinnest region → emptiest box → warm box → fewest replicas | maximum separation |

`BestFit` and `WorstFit` are the standard bin-packing names and mean the standard things, which is
easy to get backwards: **best fit consolidates, worst fit distributes.** Taking the tightest box
means the partly-used one keeps winning until it is full; taking the emptiest means the next
replica finds a different box emptiest and lands there instead. `pack` is built on `BestFit` for
exactly that reason.

Every policy ends in a scorer that breaks a remaining tie, so two boxes alike in everything a
policy reads still order deterministically — otherwise placement wanders between them run to run.
`test_every_policy_ends_in_a_total_order` pins it.

## The tuple is the tiebreak

`sort_key` returns one number per scorer, in the policy's order, and the winner is the `min`. So a
later scorer only speaks where every earlier one ties — `balanced` puts `WarmCache` first, which
means an emptier box never wins over a warm one, it only breaks a tie between two warm ones.

Ordering is therefore expressed by the tuple's order and nothing else. There is no weighting, no
normalisation and no arithmetic between scorers, because a weighted sum makes "why did it pick that
box" unanswerable at 3am, and every one of these preferences is a strict tiebreak in practice
rather than a quantity to trade off.

## What a Candidate carries, and why it is precomputed

`has_local_weights` is the sharp one. It means *a sibling replica of the same Model is already on
this box*, derived from control-plane state alone — never read back off a box, which is Grove's
rule for desired state.

It deliberately does **not** consult `Compile Cache`. That doctype registers the shared S3 bucket,
keyed on image digest, GPU and TP; a hit says any box with that image can pull the cache, not that
this box holds anything. Using it would score every box identically and mean nothing.

It is also false for a Model with `weights_s3_uri` set. Those weights stream from S3 the same way
everywhere, so no box is warmer than another, and letting `WarmCache` speak would pile every
replica onto one box for no gain.
