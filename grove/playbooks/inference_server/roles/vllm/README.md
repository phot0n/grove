# Ansible role: `vllm`

Runs [vLLM](https://docs.vllm.ai) from an engine image on an Ubuntu host with an NVIDIA GPU,
with Docker owning the container. Tuned for **Blackwell (`sm_120`, RTX PRO 6000 / B-series)**
serving FP8 models. Codifies the runbook in [`../../../README.md`](../../README.md).

## Requirements

- A **working NVIDIA driver already installed** (`nvidia-smi` must succeed). The role does
  not install or touch the driver — it fails fast if the GPU isn't visible.
- Docker + the NVIDIA container toolkit on the box. The `gpu_host` role (Inference Server →
  Setup) installs both, and points Docker's `data-root` at the data volume.
- Run with `become: true` (root). SSH access to the target.
- `ansible-playbook` on your control machine.

## What it does

1. Pulls `{{ vllm_image }}` (logging in first only when registry credentials are passed).
2. Creates inductor/triton/torch cache dirs; optionally writes an HF token.
3. Auto-generates and persists an API key (unless you supply `vllm_api_key`).
4. Optionally pre-downloads the model weights, using the image's own `hf` into the
   box-shared cache every instance mounts.
5. Renders the container's env file and run script, replaces the container when either
   moved, and waits for `/v1/models` to return `200`.
6. POSTs `vllm_warmup_request` once. A 200 from `/v1/models` only means the API server bound
   its port; this is the first thing that asks the GPU to run a forward pass, so a kernel that
   cannot run on this card fails the play here rather than on a customer's request.

All steps are idempotent (`creates:` guards on the key and the weights; the run script's
content is what decides whether the container is replaced).

There is no systemd unit. `--restart unless-stopped` in the run script already survives a
crash, a reboot and a dockerd restart, so a unit would only be a second owner of the same
restart state.

## Usage

```bash
cd grove/playbooks/inference_server
cp inventory.example.ini inventory.ini      # set ansible_host / user
ansible-playbook serve.yml -e vllm_image=vllm/vllm-openai:v0.24.0
```

The API key is stored at `{{ vllm_home }}/keys/{{ vllm_instance }}.key`. Manage the engine
with `docker ps`, `docker logs vllm-<instance>`, and re-run the role (or Grove's **Update
Engine Config**) to change its config — the run script at
`{{ vllm_home }}/containers/vllm-<instance>.sh` is that config, so editing the box by hand
is overwritten on the next run.

## Key variables

See [`defaults/main.yml`](defaults/main.yml) for the full list. Most-used:

| Variable | Default | Notes |
|---|---|---|
| `vllm_image` | `""` | **Required** — the engine image, e.g. `vllm/vllm-openai:v0.24.0` |
| `vllm_model` | — | Any HF repo vLLM supports |
| `vllm_served_name` | — | Name clients pass as `model` |
| `vllm_serve_args` | `[]` | The whole `vllm serve` flag list — see below |
| `vllm_env` | `{}` | Engine env vars, rendered as the container's `--env-file` |
| `vllm_api_key` | `""` | Blank → auto-generated + persisted |
| `vllm_hf_token` | `""` | For gated models / faster downloads |
| `vllm_predownload_model` | `true` | Fetch weights during the play |
| `vllm_wait_for_healthy` | `true` | Block until `/v1/models` returns `200` |
| `vllm_warmup` | `true` | POST one real request after the health gate |
| `vllm_warmup_request` | `{}` | `{path, body}` from `ServeCommand`; empty skips the step |

The individual serve flags (`--max-model-len`, `--gpu-memory-utilization`, the tool/reasoning
parsers, …) are **not** role variables. Grove builds the full list in
`grove/serve_command.py` (`ServeCommand`) from the Model ⊕ the Model Deployment and passes it
as `vllm_serve_args`; the Pod path uses the same builder, so the two can't drift. Running the
role by hand means passing the flags yourself:

```yaml
vllm_serve_args: ["--served-model-name", "qwen3-32b-fp8", "--host", "0.0.0.0", "--port", "8080"]
```

## Engine env vars

`vllm_env` is a plain dict rendered one `KEY=value` per line. It is where a per-box engine
workaround goes — e.g. Blackwell needed `VLLM_USE_DEEP_GEMM=0`, because the image builds
DeepGEMM and it has no recipe for that GPU's FP8 layout:

```yaml
vllm_env:
  VLLM_USE_DEEP_GEMM: "0"
```

From Grove these are the deployment's **Environment Variables** rows, layered on top of what
Grove derives (attention backend, HF token, long-max-len guard).

## Serving a different / non-Qwen model

From Grove, edit the Model — the parsers are Model fields and `ServeCommand` reads them live.
Running the role standalone, put them in `vllm_serve_args`:

```yaml
vllm_model: meta-llama/Llama-3.3-70B-Instruct
vllm_serve_args:
  - "--served-model-name"
  - "llama-3.3-70b"
  - "--tool-call-parser"
  - "llama3_json"
vllm_hf_token: "hf_..."          # gated repo
```
