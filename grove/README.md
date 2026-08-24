# `grove/` — the control plane

Grove is a Frappe app that **owns infrastructure state and tenancy**, and a Go agent
(`pathway`, its own repo) that **serves inference traffic**. This app never sits on a request
path. It provisions boxes, and it projects state onto them.

Read this with [`../CLAUDE.md`](../CLAUDE.md) (the rules) and the per-directory READMEs:
[`grove/doctype/`](grove/doctype/README.md) · [`cloud_provider/`](cloud_provider/README.md) ·
[`playbooks/`](playbooks/README.md) · [`tests/`](tests/README.md).

## The one rule everything follows

**Grove is the source of truth; a box holds a projection it never edits.** Anything a gateway
originates is only usage counters — a token count that is lost is money that is lost, which is why
gateway Redis runs `appendfsync always`. Everything else can be pushed again, so nothing is ever
read back out of a box to decide what is true.

## Two planes

| | takes | holds tenant state |
|---|---|---|
| **Gateway Server** | groups, users, keys, the global route table | yes |
| **Ingress Server** | one thing: the replica table for the boxes in its own Network | no |

The split is enforced by what each is *given*, not by a flag: an Ingress Server doctype has no
tenant fields, and the agent in ingress mode mounts no endpoint to send them to. A box behind an
ingress contributes a route row that names the ingress and never its own address, so replica
topology stays inside its VPC and several deployments behind one ingress fold into **one** row.

## Where each concern lives

| File | Owns |
|---|---|
| `pathway_sync.py` | Every push to every box. A push that left no Pathway Sync row did not happen. |
| `usage_pull.py` | Draining `usage:<prefix>` into monthly Usage Records. |
| `access.py` | Which models a user may call, as the CSV each grant record carries. |
| `serving/` | One class per engine kind: what starts it, what environment it needs, what proves it serves. |
| `fleet.py` | What a named fleet box (Gateway/Ingress) does the same way, plus the fleet-wide settings readers. |
| `naming.py` | `<prefix><n>-<region>`, e.g. `gw1-ap-south-1` — a box's name IS its DNS label and its request-id prefix. |
| `ansible.py` / `ansible_runner.py` | Running a playbook against a box, tracked as docs. |
| `tls.py` | The fleet wildcard: issue over DNS-01, renew, push. |
| `monitoring.py` / `log_relay.py` | Exporters on every box, and shipping their output. |
| `failure.py` | `@reports_failure` — a long job that dies marks its doc Broken and says why. |
| `api.py` | The whitelisted surface a customer's portal calls. |
| `net.py` / `utils.py` | Addresses, slugs, paths. |

## What gets pushed, and under which key

The agent's admin API is token-gated (`X-Grove-Admin-Token`) at
`<box>/grove-admin/{state,state-hash,usage}`. The push is **desired state, whole, and absence
prunes**: `POST state` carries any subset of the four sections (groups, users, keys, routes),
each stamped with a hash the agent stores in `grove:state_hash` and returns from `GET state-hash`.
The tick pushes only sections whose hash the box does not already hold; a wiped Redis holds no
hashes, so the next tick re-pushes everything — that IS the repair path. `users` and `keys` are
split into 256 buckets (`pathway_sync.bucket_of`) hashed independently, so one key minted re-pushes
one bucket, not the population. The full contract lives in `plan_agent_state_sync.md` at the
repo root.

```
every minute            Grove                                    box (pathway + Redis)
                          │                                        │
  build snapshot,         │──── GET /grove-admin/state-hash ──────▶│
  hash each section       │◀··· hashes the box holds ··············│
  and bucket              │                                        │
                          │  compare — all equal? stop. no log.    │
                          │                                        │
                          │──── POST /state (drift only) ─────────▶│  one MULTI:
                          │                                        │  upsert named, DEL unnamed,
                          │                                        │  store hashes — or 500 and
                          │                                        │  nothing lands
```

The compare is per fingerprint, so wire cost tracks what changed, not fleet size:

```
        desired (computed)      held (grove:state_hash)
        groups   aa11…          groups   aa11…     same → skip
        routes   bb22…          routes   bb22…     same → skip
        keys:3f  7d90…          keys:3f  9f3a…     DIFFERS → ship bucket 3f only
        users:ef dd44…          users:ef dd44…     same → skip

  minted key hashes into ONE bucket → one ~25 KB push, not the population.
  wiped Redis → held column empty → every row differs → full re-push next tick.
```

Every builder sorts by an immutable unique id (`key_hash`, doc name, deployment id) before
hashing — same DB state must serialize identically whatever order the query returns, or the
fleet gets re-pushed over row order. Order means nothing on the wire; it exists for the hash.

| Redis key | Written by | Holds |
|---|---|---|
| `key:<sha256(secret)>` | state push (keys) | whose the key is |
| `user:<name>` | state push (users) | group, own allow/deny, over-budget flag |
| `group:<name>` | state push (groups) | the model grant for everyone in it |
| `deploy:<model>` | state push (routes) | every placement of one model |
| `catalog:public` | state push (groups) | the pooled public model list |
| `grove:state_hash` | state push | per-section/bucket hashes of what the box holds |
| `usage:<prefix>` | the agent | token counters, incl. `m:<metric>:<model>` fields |
| `sticky:<session>` | the agent | session → engine, for prefix-cache reuse |
| `inflight:<engine>` | the agent | what is running right now |
| `health:<target>` | the agent | consecutive failures behind passive ejection (60s TTL) |

Access is pushed as **three** records, one per thing that can change on its own: a group edit is one
record however many members, a budget flip is one record however many keys, and the agent resolves
all three at request time.

A model id is always `<provider>/<name>` (`frappe/qwen3.5-4b`). One id, one route key, one grant —
the bare form was deliberately broken, because routing keys on `deploy:<id>` while access is matched
against the string the caller *sent*, so a route key with no matching grant is a 403 before routing
is ever consulted.

## Scheduled jobs (`hooks.py`)

| When | Job | Note |
|---|---|---|
| `*/1` | `pathway_sync.sync_projection` | hash-gated: pushes each box only what it does not already hold; a fleet in sync logs nothing |
| `*/2` | `usage_pull.pull_all` | drain is delete-on-read, so it is **1-shot, never retried** |
| `*/2` | `cloud_provider.reconcile.sync_all` | the provider owns whether a pod is up; this closes the drift |
| daily | `usage_pull.reactivate_rate_limited`, `tls.renew_fleet_certificate` | |

Nothing else pushes. A doctype hook, a provision and a pod lifecycle all just write state; the
tick carries it within a minute. The only manual paths are Gateway Server → **Full Sync**, Ingress
Server → **Sync Replicas**, and **Force Sync All** on the Pathway Sync list.

There is no separate backstop job: the hashes live on the box, so losing the store means losing
them, which the very next tick reads as drift and heals. All Projection runs serialize on one
MariaDB advisory lock, so a slow run cannot land a stale write after a newer one.

A quiet tick still leaves a trace: every in-sync check and successful push stamps the box's
`last_synced_at` (Gateway/Ingress Server). Failures never stamp it, so the timestamp going stale
means the box is unreachable or rejecting pushes — and the Failed Pathway Sync rows say why.

## Gotchas worth knowing before you touch something

- **A Single doctype never applies its JSON default** if it predates the field. Blank is a state a
  real site lands in, so a new setting either has a safe blank meaning or throws (see
  `fleet.gateway_agent_version`).
- **A saved `Password` field reads back truthy** — asterisks in the doc's own column, the value in
  `__Auth`. Test through `get_password`, never `if not self.field`.
- **`db_set` skips `validate` and fires no `on_update`**, which is why status writes use it — and why
  a path that writes status must then do by hand whatever `on_update` would have done.
- **Deploy the agent before the state that needs it.** An older binary reading a newer projection
  fails in whichever direction that field's blank means: `models` fails *closed* (403 everyone), an
  unknown route `kind` fails *open* (wrong dial).
