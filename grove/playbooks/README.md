# `playbooks/` — how a box gets built

One Ansible project per **kind of box**, named after the doctype whose boxes its playbooks run
against. `grove.utils.ansible_project_dir("Gateway Server")` is literally
`playbooks/gateway_server`, so a folder name is not a convention you can rename freely.

```
playbooks/
  gateway_server/     gateway.yml  deploy_agent.yml  deploy_tls.yml   + config.json.j2, systemd/
  ingress_server/     ingress.yml  deploy_agent.yml
  inference_server/   provision.yml  serve.yml  reconfigure.yml  container_state.yml  teardown.yml
  machine/            ping.yml  grow_root.yml  scan_gpus.yml     — box-level, no role layered on yet
  monitoring_agent/   agent.yml  config.yml  exporters.yml  push_targets.yml
  roles/              dcgm_exporter  node_exporter  fleet_tls  grove_https  install_gateway_agent  openresty
```

A project's own `roles/` is searched first, then `playbooks/roles/`. So a shared role is written once
and named from anywhere, with no copy and no symlink to keep pointing.

Two plays deliberately live in a project that is not the doctype they run for, because they are the
same work on either kind of box: `deploy_tls.yml` sits under `gateway_server/` and an **Ingress**
Server runs it with `project="Gateway Server"`, and `exporters.yml` belongs to Monitoring Agent but
runs against Inference and Gateway Server boxes.

## How a play is actually run

Never from a shell. A doctype that stands on a box mixes in `AnsibleHost` and calls:

```python
self.run_playbook("deploy_agent.yml", extravars={...})
self.run_playbook("exporters.yml", project="Monitoring Agent")   # a shared play
```

Callers name a playbook — never a path, a server type or a Machine. `project=` is only for a play
that belongs to one doctype but runs against another's boxes.

`ansible_runner.run_play` then:

1. Loads the **Machine** behind the doc and refuses without a `public_ip`.
2. Builds a **single-host inventory** from it (`ssh_user`, `ssh_port`).
3. Pins `ansible_python_interpreter=/usr/bin/python3` when the Machine has a `cloud_provider`, because
   cloud GPU images ship several pythons and auto-discovery lands on one without `python3-apt`/cffi,
   which crashes the `apt` module. Extra-vars are highest precedence, and an explicit one still wins.
4. Runs Ansible's `PlaybookExecutor` **in-process** with a custom stdout callback, which is what
   writes the tracking docs live rather than after the fact.

There is no `ansible-playbook` subprocess in this path. (`Ansible.run()` exists for ad-hoc use and is
not what buttons call — it has no Frappe tracking.)

## What you read afterwards

- **Ansible Play** — one run, its status and `rc`. `rc == 0` is success; the callers turn that into
  Active or Broken.
- **Ansible Task** — one row per task, created **as it starts**. Handler tasks included: without that
  hook, a failure inside a handler produced a play that failed with no row saying why.

## Conventions in the plays themselves

- **Idempotent, and honest about it.** `creates:` guards on anything expensive (a generated key,
  predownloaded weights); a rendered script's *content* is what decides whether a container is
  replaced.
- **`extravars` write config files whole.** `agent.env` is generated from them, so a caller that omits
  one variable does not leave the old value — it writes a **blank**. That is fatal for the admin token
  and silently wrong for a hostname, which is why `tests/test_gateway_agent.py` asserts that every
  variable a template renders is one the button passes.
- **Restart vs reload is a real distinction.** Identity and secrets (`agent.env`) make it a different
  process → restart. Tunables (`config.json`) are re-read on `SIGUSR1` → reload, so live token streams
  are untouched.
- **`systemctl reload-or-restart`, not the systemd module's `state: reloaded`.** That refuses on an
  inactive unit — and a box with a stopped gateway is the likeliest one to be having a binary shipped
  to it, so the deploy would fail on the handler with the fix already on disk, unstarted.
- **Reconcile the *running* state, not just the file.** A `blockinfile` that already matches reports
  unchanged and never notifies its handler again, so a setting can sit correct in the config and inert
  in the process for the life of the box. See "enforce persistence on the running redis" in
  `gateway_server/gateway.yml`.
- **Non-fatal cleanup is guarded, not assumed.** Stopping OpenResty on a box that never had it uses
  `failed_when: false`.
- **The binary is downloaded, never compiled.** The agent lives in its own repo; `install_gateway_agent`
  fetches a release with a `sha256:` checksum. Which release is a Grove Settings field, so a rollback
  is an edit plus a Deploy Agent — not a control-plane release.

## Running one by hand

Prefer the doctype buttons: they pass the extra-vars, and they record what happened. When you do need
a shell, do it from the bench so the paths resolve, and pass the same extra-vars the button does —
notably `admin_token`, or the play asserts its way out rather than leaving a crash-looping box.
