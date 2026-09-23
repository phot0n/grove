# `doctype/` — what each record owns

Thirty-odd doctypes, four families. The useful question about any of them is **which state does this
one own**, because Grove's rule is that state has exactly one owner and everything else derives.

## Infrastructure — a box, or a thing a box needs

| Doctype | Owns |
|---|---|
| `Machine` | An on-prem/baremetal/VM box. Cloud GPU pods are **not** Machines — see `Pod`. `gpu_interconnect` (PCIe / NVLink / Mixed) is how its cards reach each other, off `nvidia-smi topo -m`, written by Scan GPUs — a box fact, since one card type ships both ways. |
| `GPU` | One physical card or MIG slice, and the claim on it. `device_id` is exactly what `CUDA_VISIBLE_DEVICES` is given (`GPU-<uuid>`, `MIG-<uuid>`, or the index until a scan replaces it) — stable across a reseat, which the index is not. `held_by` **is** the claim: a column, taken by compare-and-swap, so it cannot outlive the card, cannot name a card that is gone, and disappears when a scan prunes the row. Held for `Draft`/`Provisioning`/`Active`/`Broken`, released on `Inactive`/`Terminated`. How a card is named, and why a rented box identifies its cards by slot: `grove/grove/doctype/gpu/README.md`. The race two placements run: `grove/placement/README.md`. |
| `GPU Type` | One record per card type, and the number every card of it reports. Named for the card the way a person says it (`T4`, `L40S`, `RTX PRO 6000 Blackwell Server Edition`) — the name **is** the display, so nothing is slugified. `resolve()` strips the vendor word to collapse nvidia-smi's `Tesla T4`, AWS's `T4` and RunPod's `NVIDIA T4` onto one record, and records every spelling it meets in `aliases`. An alias is matched **before** the name is derived, which is the only way `NVIDIA H100 80GB HBM3` reaches `H100`. `vram_gb` is seeded by the first scan and never overwritten, so a correction here survives the next one and the whole fleet follows it. |
| `Machine GPU` | The Machine form's grid of its cards (child) — a read-only mirror of `GPU`, rewritten by the same scan that reconciles them, so the two cannot disagree. |
| `Inference Server` | A Machine that serves engines. |
| `Gateway Server` | A Machine that serves customer traffic. Its name is `GROVE_GATEWAY_ID`, its DNS label, and the first part of every request id it stamps. `gateway_store` is the store Setup put its agent on and every deploy keeps — blank only until its first Setup. `is_store_writer` marks the gateways a store is pushed and drained through, tried in name order; the rest only read it. `is_in_maintenance` is the one owner of maintenance: Start/End Maintenance writes it to `config.json` (`config.yml`, SIGUSR1) and reads the box back, and every play that writes that file carries it, so a deploy never flips it. A deploy keeps the gateway on the store it is on. |
| `Gateway Store` | The one Redis a Network's gateways share (one per Network, 6379 open only to those gateways' private IPs, behind a minted password). One in-flight counter per replica is what caps a standalone box across gateways; the store holds their keys too, so a dead one takes them down. |
| `Ingress Server` | One VPC's front door: gateways dial it by name over a verified certificate; it dials replicas privately. Holds no tenant state. `is_in_maintenance` works as on a Gateway Server (both are `fleet.PathwayHost`): its `config.yml` writes the key, and the gateways' requests through it get 503. |
| `Network` | A VPC, subnet, IGW, route table and the two security groups — creates them, not just references. |
| `Region` | A provider's region code and its label, and which gateways are in it. Belongs to one `Geography`, fixed once a Network or Machine names it. |
| `Geography` | A residency boundary (`in`, `eu`), its public `endpoint`, and its own hosted zone (`fleet_zone`) with that zone's wildcard certificate (Issue Fleet Certificate; renewed daily per geography, pushed only to its boxes). Every box in it is named `<box>.<fleet_zone>`, and the endpoint is one label under that zone. Its gateways share one DNS set at that endpoint, are pushed only routes from inside it (replicas, ingresses and vendors with this `geography`, and every pod when it is the Pod Geography), and 403 a user pinned elsewhere. Every box carries a read-only `geography` copied from its Region (Machine sets it in validate; the rest `fetch_from`). Still leaves the geography: token counts to this site, and every user's email and key hash to every gateway. |
| `Cloud Provider` | One account and its credentials. |
| `SSH Key` | The keypair a box is provisioned with. |
| `Monitoring Agent` | The scraper. A box's exporters only listen; this is what polls them. |

## Serving — what actually answers a request

| Doctype | Owns |
|---|---|
| `Model` | A model, named `<provider>/<id>` off the Model ID typed at creation and frozen there. One under the Self Hosted provider names an HF repo — that is what an engine serves, and what the S3 mirror is filled from; one a vendor serves names none, because there is no engine to start. `published` means *reachable* — a live deployment, a running pod, or a complete provider — and is never a manual claim. Not an access gate. |
| `Model Price Row` | One dated COST rate on a provider's rate card (child): what the vendor charges us for that model id and counter, from that UTC day. Reference data; nothing bills from it. |
| `Model Provider` | Who serves a model. The one flagged **Self Hosted** is ours: blank-provider Models are named under it, and only its models can be deployed. Every other provider is a vendor, dialled through its URL fields: with a **Base URL** (and a key), its published models route straight to the vendor and nothing is ever deployed for them. The name is the namespace every model under it is named in. A vendor also names the `Geography` it processes in, and only gateways there route to it, so a regional front is its own provider (`openai-eu`, `bedrock-eu`) and its models are ids of their own. "EU" differs by vendor (OpenAI and Mistral include EEA/CH; Vertex is EU member states only). |
| `Model Deployment` | The logical service: one Model on one hardware shape. Owns the image, the GPUs-per-replica, and the tuning every replica of it runs. Named `MD-{#####}`, counted — several deployments of one model are normal (a second **shape**, or a rollout running old and new side by side). The name carries neither the model nor the shape: `gpus_per_replica` is editable while a name is not, so `4xh100` would go stale the first time someone re-shaped it. Deliberately **not** region-scoped: `deploy:<model>` is global, so replicas in different regions already stand in for each other, and one deployment rolls a model out everywhere. `placement_policy` names which scorers `find_placement` ranks boxes by (`grove/placement/`) — it only ORDERS boxes that could already take the replica, never admits one that could not. |
| `Model Replica` | One **replica** of a Model Deployment: which box, which cards, which port. Named `<model id>-<region>-<server>-<n>` (`qwen3-8b-ap-south-1-inf3-00007`). Re-derives its arguments at deploy time from the Engine its deployment builds — a `custom` image runs its own entrypoint and is gated at the box's proxy instead of by a key it does not enforce. Carries tuning where it **overrides** its deployment: blank or 0 there means inherit. `kv_cache_dtype`, `gpu_memory_utilization`, `max_num_seqs` and `attention_backend` are **prefilled**, so a new replica starts as an explicit override and editing the deployment will not move it — clear the field to hand ownership back. Migrated replicas were blanked, so they inherit. `kv_cache_memory` is the one tuning value Grove writes itself — learned off a profiled boot, applied while its key matches, dropped when a boot fails under it. Takes as many cards as its deployment declares — replicas of one deployment are interchangeable, which is what makes `replicas x capacity` arithmetic. |
| `Model Replica GPU` | Which GPUs that replica was **given** (child) — the run script's `--tensor-parallel-size` and CUDA pinning. Not the claim. |
| `Pod` | A standalone RunPod vLLM instance. Fully self-contained — its own spawn/sync/restart/terminate, and it registers its own endpoint with the gateways of Grove Settings' `pod_geography` — every pod serves in that one geography (blank routes none). Sends its **stored** `serve_command`, so it must be saved before it is re-spawned. `provision_at` / `terminate_at` (Up From / Down From) are a daily window the every-minute tick (`cloud_provider/schedule.py`) keeps it inside — spawned once per day while inside and unprovisioned, terminated while outside and live; a spawn re-asks the provider `provision_retries` times (the create call only); every lifecycle call leaves a `Pod Activity` row. |
| `Pod Env` / `Pod Port` | That pod's environment and its port pool (children). |
| `Engine Image` | The container image an engine is spawned from, and what only the image knows: its `engine_kind`, and for a `custom` one the warmup request (path + body) that proves it serves. Both placements read them, so the same image warms up the same way wherever it runs. |
| `Engine Image Provider` | A registry and the credentials to pull from it. One record per registry. |

## Tenancy — who may call what, and what it cost

| Doctype | Owns |
|---|---|
| `Grove User` | A person's policy: the groups they are in, their own allow/deny, their read-only `balance` (Σ Grove Credit − `spent`) and `spent` (every user is prepaid unless marked **Free**, which waives the gate but not the pricing), `rate_limited` — the control plane's verdict, written only by `pricing.settle` and mirrored on save — and an optional `geography` pin that gateways elsewhere answer 403. Belongs to the person, not the key. |
| `Grove Credit` | One ledger entry: a top-up, or a negative correction with a note. Append-only — never edited or deleted, a wrong entry is corrected by another; a control client posts one through `/api/resource`. Its `on_update` settles the user. |
| `Model Group` | A named set of models. Membership lives on `Grove User`, which lists the groups it is in — there is no member table. A user reaches the union of all of them. |
| `Grove Model Row` | One model in a grant (child). |
| `Model Group Row` | One group a user belongs to (child). |
| `Grove API Key` | One credential. Its only fact of its own is whether it has been revoked. |
| `Usage Record` | One (key, UTC day). The counter rows are the record — what the rate tables price and the reports read; no totals of their own. A name Grove holds as a Model (published or not) gets rows; the gateway's deployment-keyed metrics do not. |
| `Usage Gateway Row` | Requests one Redis — a Gateway Store, or a box on its own — served for that key that day (child), and when it was last drained. Never a box on a store: the boxes on a store share their counters, so the writer that answered the drain says nothing about who served. |
| `Lost Usage` | A drained payload a pull could not record (one key, or a whole box), with the traceback. Replayed hourly through the normal pull path on the day it was drained; `replayed`, `attempts`, `last_error` say where it stands. Never deleted. |
| `Usage Counter Row` | That day's amount of one (model, counter) (child) — the quantity a rate row prices. |
| `Model Pricing` | A model's SELL price: one Enabled doc per model, one rate per counter, effective from the UTC day it was enabled. Enabling a new one disables the last; never edited, disabled by hand or re-enabled. A Scheduled one (one per model, typed future date, editable until then) is fired by `enable_due` every minute. A counter with no row bills 0; the Model form shows a banner while no pricing is enabled. |
| `Model Pricing Rate` | One counter's sell rate (child). |
| `Gateway Spend` | What one Redis last reported about one user: the highest lifetime counter and the balance it believed in. Folded into that Redis's pushed ceiling; never billed from. |
| `Grove Settings` | Single. The fleet-wide knobs: Pod Geography (where every pod serves), DNS provider (owns every geography's zone), ACME email, pathway release and repo, monitoring. |

## Logs — what happened, and to which box

Append-only. Never read to decide anything; read to find out why.

| Doctype | Owns |
|---|---|
| `Pathway Sync` | One push run. Its list view holds **Force Sync All** — the fleet-wide force-push. |
| `Pathway Sync Row` | One TARGET in that run (child) — names the **doctype as well as the box**, because the two planes take different pushes. |
| `Gateway Deletion` | One Redis record a box still holds and should not. Every other push is an UPSERT, so without this a revoked key would keep working. |
| `Credit Discrepancy` | Where the gateway's copy of the money and the control plane disagreed (Price Drift, Balance Mismatch, Counter Reset, Overspend, Ledger Drift). One open row per kind and key, updated not piled; `resolved` closes it. Read to find out why, never to decide. |
| `Pod Activity` | One lifecycle call on a Pod — spawn attempt, restart, stop, start, terminate — with its trigger (button or tick), outcome, duration and the provider's error. Written by `PodProvisioner`, never read to decide anything. |
| `Pathway Update` | Deploys Grove Settings' Pathway Release (and Repo, both copied read-only at insert) to chosen servers, one `Pathway Update Server` row (child) at a time. The Gateways / Ingresses boxes are set once: they add every Active server of that type at insert and limit which types rows may name; rows can be added or deleted whenever it is not running. Never starts by itself. **Start** checks nothing else is running, the pin still matches, the Pending servers are Active and the release is published, then runs `_deploy_agent` per Pending row through `runner.StepHandler` — which records `agent_version` on the server. A failed deploy stops it (traceback on the doc, Error Log, notification) and puts the failed server (gateway or ingress) in maintenance to be debugged — End Maintenance on it when done; **Continue** carries on past failed rows, and re-adding a failed server as a row retries it. **Stop** takes effect between servers, never mid-deploy. Start/end times on the update and on each row. |
| `Ansible Play` | One playbook run. Its status is written by the callback and so is best-effort; what a caller acts on is Ansible's own rc, never this. |
| `Ansible Task` | One task in it, created as it starts — including handler tasks, or a failure inside a handler produces a play that failed with no row saying why. |

## Conventions these all follow

- **Box names are generated, never typed** (`grove/naming.py`): a Machine is `<prefix><n>-<region>`,
  the prefix from its Machine Type (`NAME_PREFIX`), counted per region out of `tabSeries`, and the
  server doc on it takes the Machine's name. The first label carries the region as a suffix rather than a
  namespace — `*.<zone>` covers `gw1-ap-south-1.<zone>` and nothing deeper — and a box whose Geography has a zone is
  named with it (`gw2-ap-south-1.<zone>`), so its domain shows at a glance. Request ids and agent ids carry only the
  first label (`short_name`); boxes named before this keep their short names.
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
- **A doctype standing on a box extends `Server`** (`grove/server.py`): its name is its
  Machine's (a name passed on insert wins), it calls `run_playbook("x.yml")` — never
  a path, a server type or a Machine — and it is retired with `Archive`. Archive refuses while
  anything depends on the box: each doctype lists its own dependents in `archive_blockers`
  (replicas on an Inference Server, boxes routed through an Ingress, targets of a Monitoring Agent,
  the last gateway of a region that still serves, gateways still on a Gateway Store), then a cloud Machine is terminated, whose
  cascade already marks the row Terminated and drops its DNS. An on-prem box only has its row retired.
- **A new Inference Server takes the Network's only ingress and only monitoring agent** when there
  is exactly one of each (`default_to_network_singletons`); two or more stay an operator's pick.
- **An Inference Server is fronted by an ingress or is Standalone.** Standalone skips the ingress:
  gateways dial `<short name>.<its geography's zone>` (DNS record follows the flag), its nginx serves the fleet
  wildcard, and renewals reach it through `tls.push_to_proxies`. Neither saves with a banner; Setup refuses.
- **A connections badge counts live rows.** `grove/notifications.py` names the "open" filter per
  doctype — a Network's Machine link and a Model Deployment's Model Replica link show the Active
  ones, a Pod's Activity link the failed ones — and the total stays in the count.
- **Long buttons enqueue.** `frappe.enqueue_doc(..., queue="long")`, and the worker method carries
  `@failure.reports_failure` so a crash marks the doc Broken with the reason on it.
- **A child table row names the thing it is about.** Frappe silently drops an `append()` key that is
  not a field, which is how a row full of numbers with no server on it got shipped once.
