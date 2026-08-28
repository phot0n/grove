# GPUs — what a card is, and how it is named

`GPU` is the source of truth for what hardware exists;
`Machine GPU` is a read-only mirror the Machine form draws, and `Model Replica GPU` is which cards
a replica was given. Both fetch from the card, so neither can disagree with it.

Placement, leasing and the claim race live next door in `grove/placement/README.md`. This file is
about **identity**: which string names a card, and what happens when the answer changes.

| Doctype | Owns |
|---|---|
| `GPU` | One physical card or MIG slice. `device_id`, `gpu_index`, `gpu_type`, and `held_by` — the claim. |
| `GPU Type` | One record per card type. THE VRAM figure, and every spelling the fleet has reported. |
| `GPU Type Alias` | One reported spelling (child). |
| `Machine GPU` | The Machine form's grid (child). A mirror; the scan rewrites it. |

## The claim is a field

`held_by` on the card, taken by compare-and-swap — not a row of its own.

A claim row would have to be **named after the card it holds**, and every candidate is bad: a UUID
makes an unreadable docname, a CUDA index cannot express a MIG slice, and a truncated UUID trades a
silent, data-dependent collision for brevity. A column needs no name. It also cannot outlive the
card, cannot name a card that does not exist, and goes when a scan prunes the row.

That last property is why the reconcile rules below matter so much: **deleting a GPU row deletes a
claim**, and strands every `Model Replica GPU` pointing at it.

## Two identifiers, two jobs

A card carries both, and they are not interchangeable.

| | `device_id` | `gpu_index` |
|---|---|---|
| Is | `GPU-70d1…`, `MIG-41b3…`, or a bare index as a placeholder | the CUDA slot: 0, 1, 2 |
| For | **identity and addressing** — the container runtime, the lease, the claim, every pin | **humans**: grids, the Add Replica dialog, error messages, ordering |
| Stable across | a reseat, a re-scan, a reorder | a stop/start on rented hardware |

### How a card is actually pinned

`docker run --gpus '"device=<device_id,…>"'` (`vllm-container-run.sh.j2`). The container toolkit
accepts "a comma-separated list of GPU UUID(s) or index(es)"; inside the container CUDA renumbers
whatever it was given to `0..N-1`.

**Not** `CUDA_VISIBLE_DEVICES`, and that is worth keeping:

- vLLM crashed on UUIDs there until recently — `device_id_to_physical_device_id` did
  `int(physical_device_id)` (vllm-project/vllm#32569). Going through the runtime means vLLM only
  ever sees plain indexes, whatever engine image is pinned.
- `CUDA_DEVICE_ORDER` defaults to `FASTEST_FIRST` while `nvidia-smi` enumerates by PCI bus, so on a
  mixed box an index means two different cards depending on who is asked. A UUID does not.

### `device_id` forms

| Form | When |
|---|---|
| `GPU-<uuid>` | a whole card, from `nvidia-smi --query-gpu=…,uuid` |
| `MIG-<uuid>` | a slice — **driver R470+**. Below that the runtime wants `MIG-<GPU-UUID>/<GI>/<CI>`, so a short form there addresses nothing |
| `0`, `1` | **placeholder**: no driver has been asked yet. Written by `aws.py` at provision, where AWS has no UUID to give |

A placeholder is a slot standing in for an identity, and `is_placeholder_device_id` is the one
place that judgement lives: bare digits, nothing else.

## Reconciling a scan

`plan_reconcile(existing, scanned, slot_is_identity)` is pure and decides everything;
`reconcile_gpus` only executes. Two passes:

1. **Exact `device_id`.** A real UUID matches only itself, so two cards that swapped slots keep
   their own rows.
2. **By slot.** A card matched here is *upgraded in place* — same row, new `device_id` — so the
   claim and every replica pin survive.

Who is eligible for pass 2 is the whole design:

- a **placeholder**, always. It was never an identity, so the first real scan is the same card seen
  properly for the first time.
- **every card, on a rented box** (`slot_is_identity`, set from `Machine.cloud_provider`).

### Why rented hardware is different

Stopping and starting an EC2 instance migrates it to another underlying host — AWS's own remedy for
a sick GPU is to do exactly that. It comes back with a **different physical card at the same
index**. The UUID that was identity yesterday names silicon this account no longer has.

Without the slot rule, that scan prunes the old row and inserts a new one. The card is fine; the
bookkeeping is not:

- the replica's `Model Replica GPU.gpu` dangles at a deleted record, so the replica cannot be saved
- if it is deployed, `gpu_count` comes from the child rows while the device list comes from cards
  that still exist — vLLM is handed one device and told to shard across two

On bare metal the opposite is true, so the strict rule stays: a UUID that stops answering means the
card was pulled, and absorbing a stranger into its row would repoint a replica at silicon nobody
chose. `test_gpu_reconcile.py` pins both directions against the same input — that pair is what makes
this a property of the box rather than a global loosening.

Slot identity is not a licence to keep everything: a card at a slot the scan no longer reports is
still pruned, or an instance resized to fewer GPUs would offer cards it does not have.

## `GPU Type` — the catalogue

Three sources name one card three ways (`Tesla T4`, `T4`, `NVIDIA T4`) and each writes its own VRAM,
so one T4 read 15 GB and another 16. `resolve()` collapses them: strip the vendor word, collapse
whitespace, keep the rest as written.

- **The name IS the display.** Nothing is slugified — a slug would put
  `rtx-pro-6000-blackwell-server-edition` in every grid. There is no `display_name` and no `vendor`.
- **An alias beats derivation.** Aliases are matched *before* the name is derived, which is the only
  route by which `NVIDIA H100 80GB HBM3` reaches `H100`. The table learns every spelling it meets.
- **`vram_gb` is seeded once and never overwritten.** The sources disagree, so last-write-wins would
  make the figure depend on which box was rescanned most recently. Correct it here and the fleet
  follows.
- **A MIG slice resolves to its own type**, not its parent's. Calling it an A100 would offer a
  replica 80 GB that does not exist.
- `resolve()` **creates on a miss** rather than raising: a scan meeting new silicon has to record it
  and carry on, because failing leaves the box with no inventory at all.

## Ordering

`cards_on` orders by `machine, gpu_index`, and `fitting_gpus` returns cards **in the order it was
given** rather than sorted. "Take the first N" therefore means the first N slots; sorting docnames
would pin cards by hash.

## Not built yet

- **MIG enumeration.** `scan_gpus.yml` queries `--query-gpu`, which reports the *parent*, so a MIG
  box would look like one 80 GB A100 where there are seven 10 GB slices — the fit check passes and
  the engine fails. Needs `nvidia-smi -L`, written against captured output from real hardware, plus
  the driver-version check above. No MIG box in this fleet.
Nothing else is outstanding here: the capability check below is built.

## What a card can RUN

Capacity and capability are different questions. A T4 has room for a 9 GB model and still cannot
serve it: nearly every modern repo states `bfloat16`, that needs compute capability **8.0**, and
vLLM does not fall back — it raises and exits, minutes into a play, on the box.

`GPU Type.compute_capability` is the figure, read from `nvidia-smi --query-gpu=compute_cap` and
seeded like `vram_gb` — once, never overwritten, so a correction survives a rescan. `Model.torch_dtype`
is what the repo asks for, read by Fetch Architecture. `placement_errors` compares them, and the
scheduler rejects the box before anything is pulled.

| Check | Needs | Skipped when |
|---|---|---|
| weights in bfloat16 | cc ≥ 8.0 | the card is unscanned (0) or the Model has no dtype |
| `kv_cache_dtype: fp8` | cc ≥ 8.9 | the card is unscanned (0) |

**The remedy is the point.** `dtype` is an `OVERRIDABLE` knob, so the error names a fix that works:
`float16` on the deployment, or on ONE replica when a single older box needs it. A check that could
only refuse would just move the failure earlier.

`0` means nobody has asked the card, and an unknown skips the check — the same reading a blank CPU
architecture gets. Refusing there would make every provider-seeded box unplaceable until someone
SSHed into it.

Two fetch caveats, both learned the hard way: the dtype is on the **language model** in a multimodal
repo (`text_config.dtype`) and transformers 4.57 renamed `torch_dtype` to `dtype`, so `config_dtype`
reads both spellings in both places. And a card's fetched columns do not refresh when its TYPE
changes, so `GPU Type.on_update` pushes them and a scan re-pulls them — otherwise correcting a
figure here would never reach the cards the checks actually read.
