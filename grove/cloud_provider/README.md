# `cloud_provider/` — talking to the outside

Thin clients over provider APIs, plus the two things built on top: provisioning and reconciliation.
Nothing here reads or writes a doctype except `reconcile.py`.

| File | What it is |
|---|---|
| `base.py` | `CloudClient` — the contract, and `CloudClientError`. |
| `aws.py` | `EC2Client`. |
| `runpod.py` | `RunPodClient`. |
| `dns.py` | `Route53Client`, and the two-tier record design. **Deliberately not a `CloudClient`** — see below. |
| `provisioner.py` | Turning a doc's wishes into a running instance. |
| `reconcile.py` | The `*/2` job: the provider owns whether an instance is up, so this closes the drift lifecycle jobs leave behind. |

## Why Route53 is not a CloudClient

`CloudClient` is **one account in one region**, and every method on it is abstract. Route53 is global,
and adding its methods to that contract would break `RunPodClient`, which cannot implement any of
them. So `Route53Client` stands alone and the doctype assembles its arguments.

## The two DNS tiers

Customer traffic resolves through two record sets, because a Route53 **latency** RRSet is keyed on
(name, type, **region**) and therefore holds exactly one row per region — which capped the fleet at
one gateway per region until this existed.

```
api.<zone>              CNAME, latency, one row per region, health-checked by that region's
  SetId=ap-south-1        calculated check          → ap-south-1.api.<zone>
  SetId=us-east-1                                   → us-east-1.api.<zone>

ap-south-1.api.<zone>   A, MULTIVALUE ANSWER, one row per GATEWAY, each with its own check
  SetId=gw1-ap-south-1  HealthCheckId=hc-1          → 13.x.x.x
  SetId=gw2-ap-south-1  HealthCheckId=hc-2          → 13.y.y.y
```

One record per IP is the escape from "one health check per record": a multivalue row carries a single
value and a single check, and Route53 drops the unhealthy rows out of the answer.

**Ownership.** A Gateway Server owns its own name record, its multivalue row and its endpoint health
check. Its **Region** owns the calculated check and the latency row — one row stands for every
gateway in it, so the first gateway in creates the pair and the last one out removes it
(`Region.sync_gateway_dns`).

**The calculated check is what makes the latency tier honest.** Its children are that region's
gateway checks at `HealthThreshold=1` — up while any one gateway is up. Without it, latency keeps
answering with a region whose gateways have all died. Chosen over an alias with
`EvaluateTargetHealth`, which is also healthy-if-any but fails *silently open*: a child row missing a
check counts as healthy. That is also why the latency row is a **CNAME** rather than an alias — a
CNAME carries `HealthCheckId` outright instead of inferring health from its target.

## Two settings, both default on, both off for development

| Grove Settings | Off means |
|---|---|
| `gateway_latency_routing` | No region tier. Every gateway's row sits in one multivalue set **at** `gateway_host` — no CNAME hop, and `Region.latency_reference` is never read, so a Region named `local` needs nothing. |
| `gateway_health_checks` | No checks written at all. Both tiers still resolve, because Route53 counts an unchecked record as healthy. Only ejection is lost, and nothing is billed. |

## Rules Route53 enforces that the code is shaped around

- **A DELETE must repeat the record exactly as written** — value, TTL, routing policy, health check.
  A DELETE that does not match leaves the record in place, which is a black hole for whatever share
  of customers resolve to a box that is gone. Rows whose old shape cannot be reconstructed are listed
  and deleted **verbatim**.
- **A routing policy cannot be UPSERTed into another one.** Switching latency routing off finds the
  *same* record set under a different policy: it has to be deleted and written again, not updated.
  Switching it back on finds a *different* record set, because the replacement lives one name down —
  that delete rides along in the same batch as the writes, so the shared name is never answerless.
- **A CNAME cannot sit beside a record of any other type at the same name.** So simple mode does not
  merely skip the region tier, it must tear it down *before* writing A records there.
- **A health check cannot be deleted while a record set or a calculated check still names it.** Rows
  come off first, always. Getting this backwards leaks a billed check on every terminate and nothing
  surfaces it.
- **A calculated check with no children and a threshold of 1 evaluates UNHEALTHY** — so a region whose
  gateways carry no checks writes its row check-less rather than creating one.
- **A latency row's `Region` must be an AWS region name.** A `Region` is named with its *provider's*
  code, and RunPod's `EU-RO-1` is not one — hence `Region.latency_reference`, which is only a
  reference point and names the nearest AWS region.
- **`CallerReference` is the idempotency token for a health check.** A crash between the create and
  the `db_set` that remembers the id would orphan a billed check answering to nobody *and* block its
  own retry forever, so `HealthCheckAlreadyExists` is recovered by scanning for the reference.

`HEALTH_CHECK_INTERVAL` / `HEALTH_CHECK_FAILURES` beside `TTL` are the whole failover knob: 30×3 + 60s
TTL is ~150s of stale answers at base price; 10×2 is ~50s at +$1/mo per check. Changing them also
needs `update_health_check` on the checks that already exist — nothing reconciles that.
