# `serving/` — what starts an engine

One class per engine kind, behind one contract. A placement (Pod, Model Deployment) asks an engine
what to run, what environment it needs, what proves it serves, and what the routing side may hold
it to — and never branches on `engine_kind` itself.

**Nothing here imports frappe.** The Model's launch config arrives as a plain mapping
(`Model.launch_config`) and the placement's tuning as keyword arguments, so every test in this
package is pure: no site, no mocking.

| File | What it is |
|---|---|
| `base.py` | `Engine` — the contract, `EngineError`, and `engine_class` / `build_engine`. |
| `vllm.py` | `VllmEngine` — an image whose entrypoint takes `vllm serve` arguments. |
| `custom.py` | `CustomEngine` — an image that serves itself. |

## Adding an engine

One file, one entry in `engine_class`'s dict. `Pod`, `Model Deployment` and `pathway_sync` do not
change. The kind string is the `Engine Image.engine_kind` Select option, verbatim and lowercase.

## What belongs on the contract

Everything on `Engine` is either abstract or genuinely shared arithmetic, and the test for the
former is whether **every** engine can answer it. `CustomEngine` answers most of them with an
absence — `""`, `{}`, `[]`, `False` — and those absences are the design: they are what four
`is_custom_engine` branches used to say, moved to where the question is asked.

`placement_errors` is the sharp case. It is abstract, and `CustomEngine` returns `[]`, because
vLLM's rules — attention heads dividing by tensor-parallel size, the whole model fitting in VRAM —
assume an engine that shards and loads the way vLLM does. A "neutral" shared implementation would
start failing containers that have been serving fine.

`warmup_request` is the one thing a custom image is asked to state rather than answer with an
absence, and it is stated on the **Engine Image** (`warmup_path`, `warmup_body`) — which request
proves an engine serves is a fact about the image, not about where it is placed, so a Pod and a
Model Deployment of the same image warm up the same way. `VllmEngine` derives its own and ignores
both fields; a custom image that names no path has no warmup, and the health gate is the whole
proof.

`args` and `repo` are both on the contract because the on-prem run script renders them into
*separate* slots: the positional unquoted, every flag quoted. That quoting is also what stops an
operator's Startup Command reaching the shell, so the two must not be collapsed into one string.

## Context length is typed, not looked up

`Max Model Len` on a Pod or a Model Deployment is a `Data` field, and `parse_context_length` in
`base.py` turns what was typed into tokens: `32k` → 32768, `128k` → 131072, `1m` → 1048576. The
multiplier is 1024 and never 1000 — that is the number a repo's `config.json` declares, and the
only one vLLM accepts without `VLLM_ALLOW_LONG_MAX_MODEL_LEN`. A bare number is already tokens, so
every placement saved before the suffix existed reads back unchanged.

Parsed once, in `Engine.__init__`, so both placements get it from the one builder they already
share, and each `validate` writes the parsed number back onto the field — what is stored is
what reaches `--max-model-len`, and the suffix is only how it was typed. Blank stays blank:
that is how a placement asks for the engine default. Anything that is not a context length raises `EngineError` while the doc is being saved,
rather than being handed to `--max-model-len` and failing minutes later inside a play.

## Order is load-bearing in `env()`

`docker --env-file` is line-ordered and the on-prem template iterates `.items()`, so reordering the
keys re-renders the env file, which fires `notify: recreate vllm container` and replaces every
container in the fleet. `VllmEngine.env` inserts in the order the box already has, and a test pins
it.
