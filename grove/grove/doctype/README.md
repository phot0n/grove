# `doctype/` — what each record owns

Thirty-odd doctypes, four families. The useful question about any of them is **which state does this
one own**, because Grove's rule is that state has exactly one owner and everything else derives.

## Infrastructure — a box, or a thing a box needs

| Doctype | Owns |
|---|---|
| `Machine` | An on-prem/baremetal/VM box. Cloud GPU pods are **not** Machines — see `Pod`. |
| `Machine GPU` | One GPU on a Machine (child). |
| `Inference Server` | A Machine that serves engines. |
| `Gateway Server` | A Machine that serves customer traffic. Its name is `GROVE_GATEWAY_ID`, its DNS label, and the first part of every request id it stamps. |
| `Ingress Server` | One VPC's front door: gateways dial it by name over a verified certificate; it dials replicas privately. Holds no tenant state. |
| `Network` | A VPC, subnet, IGW, route table and the two security groups — creates them, not just references. |
| `Region` | A provider's region code, plus the AWS region its latency records are measured against. Owns the DNS tier above the gateways. |
| `Cloud Provider` | One account and its credentials. |
| `SSH Key` | The keypair a box is provisioned with. |
| `Monitoring Agent` | The scraper. A box's exporters only listen; this is what polls them. |

## Serving — what actually answers a request

| Doctype | Owns |
|---|---|
| `Model` | A model, named `<provider>/<id>`. `published` means *reachable* — it tracks whether a live route exists and is never a manual claim. Not an access gate. |
| `Model Provider` | Who serves a model: `frappe` for our own engines, a vendor for a third-party API. The name is the whole record. |
| `Model Deployment` | One on-prem placement of a Model on an Inference Server. Re-derives its arguments at deploy time. |
| `Model Deployment GPU` | Which GPUs that placement takes (child). |
| `Pod` | A standalone RunPod vLLM instance. Fully self-contained — its own spawn/sync/restart/terminate, and it registers its own endpoint. Sends its **stored** `serve_command`, so it must be saved before it is re-spawned. |
| `Pod Env` / `Pod Port` | That pod's environment and its port pool (children). |
| `Engine Image` | The container image an engine is spawned from. |
| `Engine Image Provider` | A registry and the credentials to pull from it. One record per registry. |

## Tenancy — who may call what, and what it cost

| Doctype | Owns |
|---|---|
| `Grove User` | A person's policy: their group, their own allow/deny, their monthly token budget, whether they are currently over it. Belongs to the person, not the key. |
| `Grove User Group` | A named set of models. Membership lives on `Grove User`, which links here — there is no member table. |
| `Grove Model Row` | One model in a grant (child). |
| `Grove API Key` | One credential. Its only fact of its own is whether it has been revoked. |
| `Usage Record` | One (key, month). Totals roll up **from** the child rows, so nothing is lost. |
| `Usage Gateway Row` | That month's tokens from one gateway (child). Summed across gateways. |
| `Usage Model Row` | That month's tokens per model (child). |
| `Grove Settings` | Single. The fleet-wide knobs: shared name, zone, DNS provider, agent release, latency routing, health checks, ACME, monitoring. |

## Logs — what happened, and to which box

Append-only. Never read to decide anything; read to find out why.

| Doctype | Owns |
|---|---|
| `Agent Sync` | One push run. |
| `Agent Sync Row` | One TARGET in that run (child) — names the **doctype as well as the box**, because the two planes take different pushes. |
| `Gateway Deletion` | One Redis record a box still holds and should not. Every other push is an UPSERT, so without this a revoked key would keep working. |
| `Ansible Play` | One playbook run. |
| `Ansible Task` | One task in it, created as it starts — including handler tasks, or a failure inside a handler produces a play that failed with no row saying why. |

## Conventions these all follow

- **Server names are generated, never typed** (`grove/naming.py`): `<prefix><n>-<region>`, counted per
  region out of `tabSeries`. A name is one DNS label, so the region is a suffix rather than a
  namespace — `*.<zone>` covers `gw1-ap-south-1.<zone>` and nothing deeper.
- **A cloud resource id lives in a read-only `Data` field**, written with `db_set`, and its creator is
  guarded by `if self.<id>: return` (see `Network.create_network`). That is what makes provisioning
  re-runnable.
- **A doctype standing on a box mixes in `AnsibleHost`** and calls `run_playbook("x.yml")` — never a
  path, a server type or a Machine.
- **Long buttons enqueue.** `frappe.enqueue_doc(..., queue="long")`, and the worker method carries
  `@failure.reports_failure` so a crash marks the doc Broken with the reason on it.
- **A child table row names the thing it is about.** Frappe silently drops an `append()` key that is
  not a field, which is how a row full of numbers with no server on it got shipped once.
