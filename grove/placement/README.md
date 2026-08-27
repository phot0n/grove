# `placement/` — which box a replica goes on

One class per preference, composed in order by a named policy. `Model Deployment` never names a
concrete Scorer or branches on the policy string — `placement_policy` is the one place that
dispatch happens, the same way `engine_class` is for engines.

**Ranking imports no frappe.** A `Candidate` arrives already measured against one deployment's
shape, so `base.py` and `scorers.py` test pure: no site, no mocking, no fixtures. `lease.py` is the
exception and says why below.

| File | What it is |
|---|---|
| `base.py` | `Candidate`, the `Scorer` contract, `sort_key`, and the `placement_policy` registry. |
| `scorers.py` | Every preference. One class, one `score`, a few lines each. |
| `lease.py` | The Redis hint that keeps two placements off one card. Advisory; owns nothing. |

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


---

# Two placements, one card

Ranking decides *which* box. This decides what happens when two placements pick the same one at the
same time. Three layers, and only the middle one is authoritative:

| Layer | Where | Owns | Losing it costs |
|---|---|---|---|
| Lease | Redis, TTL `60s` | nothing — a hint that someone is mid-placement | a rival blocks instead of skipping; still correct |
| **`GPU Claim`** | **MariaDB, name `<machine>:<index>`** | **the card** | **two engines on one card** |
| `Model Replica GPU` | MariaDB, child row | which cards this replica was *given* — config for the run script | a wrong `--tensor-parallel-size` |

## The claim is the mutex

`GPU Claim` is named for the resource, so taking a card is `INSERT` and the primary key is what
arbitrates. A second insert raises `DuplicateEntryError`; `add_replica` catches it and moves to the
next box `ranked_placements()` already ranked. **No lock is taken anywhere.**

Named for the **machine**, not the Inference Server: a GPU is a `Machine GPU` row and
`Inference Server.machine` is neither unique nor checked, so two servers naming one machine would
otherwise each claim card 0 of the same physical box.

This is the Kubernetes/Nomad shape — decide on a possibly-stale view, let one authoritative write
be the truth, requeue the loser. Nomad's docs put it plainly: schedulers run "without locking or
reservations", and the leader rejects conflicting plans. Mesos is the counter-example worth
knowing: it *did* lock resources into exclusive offers, deadlocked gang-scheduled jobs through
resource hoarding, and filed MESOS-1607 to move "from mutual exclusion toward optimistic
competition".

## Why a lease exists at all

Two facts about a database claim, both measured on a real bench, not assumed:

1. **An uncommitted claim blocks rivals rather than rejecting them.** InnoDB locks the unique index
   entry for an in-flight `INSERT`, so a second placement on the same card does not fail fast — it
   waits out the winner's *entire transaction*, holding a connection. Frappe commits at the end of
   the request, so that was once the whole of `add_replica` including `setup()`. `_place` now
   commits immediately after claiming, before anything slow, which shrinks the window to the insert.
2. **A committed claim is still invisible under `REPEATABLE READ`.** MariaDB's default here. A
   transaction that read before the claim committed keeps its snapshot and re-reads free. So no
   isolation level makes an in-flight placement visible, and `READ COMMITTED` would only help
   *after* the commit.

The lease covers both. It is written **before** the row and read outside any transaction, so a
rival sees it immediately, skips the card, and never queues.

## Authoritative versus advisory, and why the split

Losing the lease is survivable; losing the claim is not. Redis here is `allkeys-lru` with
`appendonly no` and `save ""` — any key evictable, nothing persisted. If it is flushed, placement
degrades to the behaviour it had before leases existed: correct, just blocking. If *ownership*
lived there, a flush would report every card in the fleet free and the next placement would
double-book all of them, silently.

So the hint lives where losing it is cheap, and ownership lives where it is durable and
transactional with the replica that holds it.

## When a claim is held

`CLAIM_HOLDING_STATUSES` = `Draft` + `GPU_CLAIMING_STATUSES` (`Provisioning`, `Active`, `Broken`).

`Draft` holding is the point: a replica takes its cards the moment its row exists, before anything
is deployed. `Inactive` releases — a stopped container holds no VRAM — and `start()` re-takes them,
failing loudly if a sibling won meanwhile. `Broken` keeps them, because `--restart unless-stopped`
means a crash-looping engine comes back onto its cards.

Status moves by `db.set_value`, which never runs `validate`, so claims are settled by
`sync_gpu_claims()` after each transition rather than in the controller.

## Failure modes

| What happens | Result |
|---|---|
| Redis flushed or evicted | leases vanish; rivals block on the insert again; correctness unaffected |
| Worker dies mid-placement | lease expires by TTL; the savepoint rolled the replica back |
| Worker dies after the claim, before releasing | card stranded — `release_if_stale` frees it when someone next wants it |
| Draft replica abandoned | cards held until it is deleted; the allocation panel names the holder |
| Two placements, same card | one wins on the primary key; the other takes the next ranked box |

## Traps

- **`leased()` uses `get`, never `exists`.** `RedisWrapper` leaves `set`/`get`/`delete` as the raw
  client but overrides `exists()` to run the name through `make_key` (which prefixes `db_name`). Mix
  them and the lease is written and read under different keys, so every card reports free and the
  skip does nothing. That bug passed every unit test and was only caught against live Redis.
- **`LEASE_TTL = 60` is a guess.** It has to outlive a placement plus the stale-read window of a
  transaction that started just before it. Nothing measures either.
- **Lease keys are not namespaced per site.** One Grove site per bench is fine; two would share
  these keys, and a machine name colliding across them would have one site skip the other's cards.
- **No fairness.** Optimistic competition has no notion of it — slab 4's autoscaler placing in a
  loop will beat an operator's button press every time. Omega's documented downside, unsolved here.
