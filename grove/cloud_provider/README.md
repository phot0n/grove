# `cloud_provider/` — talking to the outside

Thin clients over provider APIs, plus the two things built on top: provisioning and reconciliation.
Nothing here reads or writes a doctype except `reconcile.py`.

| File | What it is |
|---|---|
| `base.py` | `CloudClient` — the contract, and `CloudClientError`. |
| `aws.py` | `EC2Client`. |
| `runpod.py` | `RunPodClient`. |
| `dns.py` | `Route53Client`, and the two-tier record design. **Deliberately not a `CloudClient`** — see below. |
| `provisioner.py` | Turning a doc's wishes into a running instance. A spawn retries only the provider's create call, up to the Pod's `provision_retries`, `SPAWN_RETRY_DELAY` apart; every lifecycle call leaves a `Pod Activity` row, and `fail()` writes the terminal one. |
| `schedule.py` | The `*/1` job: keeps each Pod inside its daily Up From / Down From window — one scheduled spawn per window (`scheduled_for` marks the day, so a spawn that gave up or a Stop pressed by hand holds), a terminate whenever a live pod is outside it. Jobs are de-duplicated per pod. |
| `reconcile.py` | The `*/2` job: the provider owns whether an instance is up, so this closes the drift lifecycle jobs leave behind. |

## Why Route53 is not a CloudClient

`CloudClient` is **one account in one region**, and every method on it is abstract. Route53 is global,
and adding its methods to that contract would break `RunPodClient`, which cannot implement any of
them. So `Route53Client` stands alone and the doctype assembles its arguments.

## Gateway DNS: one multivalue set per Geography

A Geography's gateways share one record set at its endpoint (`Geography.endpoint`, e.g. `eu.<zone>`),
so each geography is its own set:

```
in.<zone>   A, MULTIVALUE ANSWER, one row per GATEWAY in that geography, each with its own check
  SetId=gw1-ap-south-1  HealthCheckId=hc-1   → 13.x.x.x
  SetId=gw2-ap-south-1  HealthCheckId=hc-2   → 13.y.y.y
```

One record per IP is the escape from "one health check per record": a multivalue row carries a single
value and a single check, and Route53 drops the unhealthy rows out of the answer. With **every** row
unhealthy it still answers with up to eight of them, so the last gateway is never dropped from DNS —
a gateway in maintenance still receives clients, and they get its 503 with Retry-After.

**Ownership.** A Gateway Server owns its own name record, its multivalue row and its endpoint health
check, which is always created (`GatewayServer.ensure_health_check`) and probes pathway's `/healthz`
on :80. An ingress gets only its own name record.

## Rules Route53 enforces that the code is shaped around

- **A DELETE must repeat the record exactly as written** — value, TTL, routing policy, health check.
  A DELETE that does not match leaves the record in place, which is a black hole for whatever share
  of customers resolve to a box that is gone. Rows whose old shape cannot be reconstructed are listed
  and deleted **verbatim**.
- **A routing policy cannot be UPSERTed into another one.** A box whose row at the shared name was
  written as a latency record finds the *same* record set under a different policy: it is deleted on
  its own and written again, not updated (`_replace_other_policy_row`).
- **A health check cannot be deleted while a record still names it.** Rows come off first, always.
  Getting this backwards leaks a billed check on every terminate and nothing surfaces it.
- **`CallerReference` is the idempotency token for a health check.** A crash between the create and
  the `db_set` that remembers the id would orphan a billed check answering to nobody *and* block its
  own retry forever, so `HealthCheckAlreadyExists` is recovered by scanning for the reference.

`HEALTH_CHECK_INTERVAL` / `HEALTH_CHECK_FAILURES` beside `TTL` are the whole failover knob: 30×3 + 60s
TTL is ~150s of stale answers at base price; 10×2 is ~50s at +$1/mo per check. Changing them also
needs `update_health_check` on the checks that already exist — nothing reconciles that.
