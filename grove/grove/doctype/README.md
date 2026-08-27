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
| `Model` | A model, named `<provider>/<id>` off the Model ID typed at creation and frozen there. One we host names an HF repo — that is what an engine serves, and what the S3 mirror is filled from; one a vendor serves names none, because there is no engine to start. `published` means *reachable* — a live deployment, a running pod, or a complete provider — and is never a manual claim. Not an access gate. |
| `Model Provider` | Who serves a model: `frappe` for our own engines, a vendor for a third-party API. The name is the namespace every model under it is named in. A **Base URL** is what makes it third-party: with one (and a key), its published models route straight to the vendor and nothing is ever deployed for them. |
| `Model Deployment` | The logical service: one Model on one hardware shape. Owns the image, the GPUs-per-replica, and the tuning every replica of it runs. Named `MD-{#####}`, counted — several deployments of one model are normal (a second **shape**, or a rollout running old and new side by side). The name carries neither the model nor the shape: `gpus_per_replica` is editable while a name is not, so `4xh100` would go stale the first time someone re-shaped it. Deliberately **not** region-scoped: `deploy:<model>` is global, so replicas in different regions already stand in for each other, and one deployment rolls a model out everywhere. |
| `Model Replica` | One **replica** of a Model Deployment: which box, which cards, which port. Named `<model id>-<region>-<server>-<n>` (`qwen3-8b-ap-south-1-inf3-00007`). Re-derives its arguments at deploy time from the Engine its deployment builds — a `custom` image runs its own entrypoint and is gated at the box's proxy instead of by a key it does not enforce. Carries tuning where it **overrides** its deployment: blank or 0 there means inherit. `kv_cache_dtype`, `gpu_memory_utilization`, `max_num_seqs` and `attention_backend` are **prefilled**, so a new replica starts as an explicit override and editing the deployment will not move it — clear the field to hand ownership back. Migrated replicas were blanked, so they inherit. Takes as many cards as its deployment declares — replicas of one deployment are interchangeable, which is what makes `replicas x capacity` arithmetic. |
| `Model Replica GPU` | Which GPUs that replica takes (child). |
| `Pod` | A standalone RunPod vLLM instance. Fully self-contained — its own spawn/sync/restart/terminate, and it registers its own endpoint. Sends its **stored** `serve_command`, so it must be saved before it is re-spawned. |
| `Pod Env` / `Pod Port` | That pod's environment and its port pool (children). |
| `Engine Image` | The container image an engine is spawned from, and what only the image knows: its `engine_kind`, and for a `custom` one the warmup request (path + body) that proves it serves. Both placements read them, so the same image warms up the same way wherever it runs. |
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
| `Pathway Sync` | One push run. Its list view holds **Force Sync All** — the fleet-wide force-push. |
| `Pathway Sync Row` | One TARGET in that run (child) — names the **doctype as well as the box**, because the two planes take different pushes. |
| `Gateway Deletion` | One Redis record a box still holds and should not. Every other push is an UPSERT, so without this a revoked key would keep working. |
| `Ansible Play` | One playbook run. Its status is written by the callback and so is best-effort; what a caller acts on is Ansible's own rc, never this. |
| `Ansible Task` | One task in it, created as it starts — including handler tasks, or a failure inside a handler produces a play that failed with no row saying why. |

## Conventions these all follow

- **Server names are generated, never typed** (`grove/naming.py`): `<prefix><n>-<region>`, counted per
  region out of `tabSeries`. A name is one DNS label, so the region is a suffix rather than a
  namespace — `*.<zone>` covers `gw1-ap-south-1.<zone>` and nothing deeper.
- **A replica's name says what it serves and where** (`grove/naming.py`):
  `<model id>-<region>-<server>-<n>`, e.g. `qwen3-8b-ap-south-1-inf3-00007`. The region goes in as
  its provider codes it — no shortening rule, since `ap-south-1`, `asia-south1` and `southeastasia`
  share none. The box contributes only the part of its name that is not the region (`inf3`), and
  the number comes out of the same `MD-` series the old `MD-00007` names used — kept as `MD-`
  through the rename because it is a counter key, not a label. The name is also the engine's
  container name (`vllm-<name>`) and its path on the box's proxy, so it is never renamed after
  insert.
- **A deployment's name is `MD-{#####}`** (`grove/naming.py`), off the SAME `MD-` series, so one
  number is handed out once and an `MD-` name never means two things — replicas predating the
  descriptive format are still named `MD-00010`, and a route row's `deployment=` carries a replica
  name. It says neither the model nor the shape: `gpus_per_replica` is editable and a name is not.
- **A cloud resource id lives in a read-only `Data` field**, written with `db_set`, and its creator is
  guarded by `if self.<id>: return` (see `Network.create_network`). That is what makes provisioning
  re-runnable.
- **A doctype standing on a box mixes in `AnsibleHost`** and calls `run_playbook("x.yml")` — never a
  path, a server type or a Machine.
- **Long buttons enqueue.** `frappe.enqueue_doc(..., queue="long")`, and the worker method carries
  `@failure.reports_failure` so a crash marks the doc Broken with the reason on it.
- **A child table row names the thing it is about.** Frappe silently drops an `append()` key that is
  not a field, which is how a row full of numbers with no server on it got shipped once.
