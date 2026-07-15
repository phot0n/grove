# Ansible role: `vllm`

Installs and runs [vLLM](https://docs.vllm.ai) as a systemd service on an Ubuntu host with
an NVIDIA GPU. Tuned for **Blackwell (`sm_120`, RTX PRO 6000 / B-series)** serving FP8
models. Codifies the runbook in [`../../../README.md`](../../README.md).

## Requirements

- Ubuntu 24.04 (or a host where `python3.12` + `gcc-13` are installable).
- A **working NVIDIA driver already installed** (`nvidia-smi` must succeed). The role does
  not install or touch the driver — it fails fast if the GPU isn't visible.
- Run with `become: true` (root). SSH access to the target.
- `ansible-playbook` on your control machine.

## What it does

1. Installs build deps (`gcc-13`, `g++-13`, `python3.12-dev`, …) — needed for vLLM's
   runtime kernel compilation.
2. Creates a venv at `{{ vllm_venv }}` and `pip install vllm` (cu13 wheels on a CUDA-13 box).
3. Creates inductor/triton/torch cache dirs; optionally writes an HF token.
4. Auto-generates and persists an API key (unless you supply `vllm_api_key`).
5. Optionally pre-downloads the model weights.
6. Templating `vllm.service`, enables + starts it, waits for `/v1/models` to return `200`.

All steps are idempotent (`creates:` guards on venv, key, and model download).

## Usage

```bash
cd deploy/vllm/ansible
cp inventory.example.ini inventory.ini      # set ansible_host / user
ansible-playbook playbook.yml
```

After the run the API key is printed-by-path and stored at `{{ vllm_home }}/api_key.txt`
on the host. Manage the service with `systemctl {status,restart} vllm` and
`journalctl -u vllm -f`.

## Key variables

See [`defaults/main.yml`](defaults/main.yml) for the full list. Most-used:

| Variable | Default | Notes |
|---|---|---|
| `vllm_model` | `Qwen/Qwen3-32B-FP8` | Any HF repo vLLM supports |
| `vllm_served_name` | `qwen3-32b-fp8` | Name clients pass as `model` |
| `vllm_max_model_len` | `40960` | >native needs YaRN in `vllm_extra_serve_args` |
| `vllm_gpu_memory_utilization` | `0.92` | Fraction of VRAM |
| `vllm_api_key` | `""` | Blank → auto-generated + persisted |
| `vllm_hf_token` | `""` | For gated models / faster downloads |
| `vllm_version` | `""` | Blank → latest; or pin e.g. `0.22.1` |
| `vllm_predownload_model` | `true` | Fetch weights during the play |
| `vllm_extra_serve_args` | `[]` | e.g. `["--kv-cache-dtype fp8"]` |

### Switching to flashinfer (best Blackwell FP8 perf)

The default avoids flashinfer because its JIT compile needs a unified CUDA toolkit the
pip install doesn't provide. After installing a matching CUDA 13 toolkit (with a real
`CUDA_HOME`), set:

```yaml
vllm_attention_backend: FLASHINFER
vllm_use_flashinfer_sampler: "1"
```

## Serving a different / non-Qwen model

Override the parsers too, e.g. for a plain instruct model with no reasoning channel:

```yaml
vllm_model: meta-llama/Llama-3.3-70B-Instruct
vllm_served_name: llama-3.3-70b
vllm_reasoning_parser: ""        # disable
vllm_tool_call_parser: llama3_json
vllm_hf_token: "hf_..."          # gated repo
```
