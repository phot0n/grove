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
| `vllm_served_name` | `qwen3-32b-fp8` | Name clients pass as `model` (unit Description only) |
| `vllm_serve_args` | `[]` | The whole `vllm serve` flag list — see below |
| `vllm_api_key` | `""` | Blank → auto-generated + persisted |
| `vllm_hf_token` | `""` | For gated models / faster downloads |
| `vllm_version` | `""` | Blank → latest; or pin e.g. `0.22.1` |
| `vllm_predownload_model` | `true` | Fetch weights during the play |

The individual serve flags (`--max-model-len`, `--gpu-memory-utilization`, the tool/reasoning
parsers, …) are **not** role variables. Grove builds the full list in
`grove/serve_command.py` (`ServeCommand`) from the Model ⊕ the Model Deployment and passes it
as `vllm_serve_args`; the container placement (Pod) uses the same builder, so the two can't
drift. Running the role by hand means passing the flags yourself:

```yaml
vllm_serve_args: ["--served-model-name", "qwen3-32b-fp8", "--host", "0.0.0.0", "--port", "8080"]
```

### Switching to flashinfer (best Blackwell FP8 perf)

The default avoids flashinfer because its JIT compile needs a unified CUDA toolkit the
pip install doesn't provide. After installing a matching CUDA 13 toolkit (with a real
`CUDA_HOME`), set:

```yaml
vllm_attention_backend: FLASHINFER
vllm_use_flashinfer_sampler: "1"
```

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
