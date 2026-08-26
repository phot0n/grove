### Grove

An Inference Platform.

Grove is the **control plane**: it provisions boxes and projects state onto them. The **data plane** is
a Go agent, `pathway`, which lives in its own repo — this app never sits on a request path.

### Where things are

| | |
|---|---|
| [`grove/`](grove/README.md) | The map: the two planes, what gets pushed under which Redis key, the scheduled jobs, the traps. **Start here.** |
| [`grove/grove/doctype/`](grove/grove/doctype/README.md) | All thirty-odd records, grouped by what state each one owns. |
| [`grove/cloud_provider/`](grove/cloud_provider/README.md) | Provider clients, and the two DNS tiers with the Route53 rules they are shaped around. |
| [`grove/playbooks/`](grove/playbooks/README.md) | How a box gets built, and how a play is invoked and tracked. |
| [`grove/tests/`](grove/tests/README.md) | Pure vs site-backed, and the rollback trap between them. |
| [`CLAUDE.md`](CLAUDE.md) | The rules for changing this repo. |

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app grove
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/grove
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
