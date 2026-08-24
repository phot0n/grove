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

`args` and `repo` are both on the contract because the on-prem run script renders them into
*separate* slots: the positional unquoted, every flag quoted. That quoting is also what stops an
operator's Startup Command reaching the shell, so the two must not be collapsed into one string.

## Order is load-bearing in `env()`

`docker --env-file` is line-ordered and the on-prem template iterates `.items()`, so reordering the
keys re-renders the env file, which fires `notify: recreate vllm container` and replaces every
container in the fleet. `VllmEngine.env` inserts in the order the box already has, and a test pins
it.
