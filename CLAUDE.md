## Main Rules

- Group related files in folders instead of adding many same-prefix modules.
- Avoid lazy re-exports in package `__init__.py` when autocomplete matters.
- Keep comments short. Remove comments that restate the code.
- Do not put comments at the top of a file. Use a short, terse class or method docstring instead.
- Do not create or commit plan/planning markdown files (e.g. `plan_*.md`); keep them out of git.

## Design Expectations

Use object-owned syntax when adding features:

```python
bench = Server().bench("main")
site = bench.site("site.local")
InstallAppTask.queue(bench, site="site.local", apps=["erpnext"])
```

Avoid new APIs that pass a bench and site into unrelated helper objects when the operation can live under `bench`, `site`, `app`, or `server`.

## Code Taste

These rules are mandatory for agents changing this repo:

- Choose clean code over clever code.
- Prefer explicit config over implicit behavior.
- Prefer object-oriented code where it maps to the domain.
- Keep functions small. Around 25 lines is a useful target, not a reason to split readable code blindly.
- Keep cyclomatic complexity <= 8
- Keep files between 100 and 500 lines when practical.
- Avoid crowded modules. If a folder grows too large, group related files into a subfolder instead of adding more same-prefix files.
- Avoid abbreviations.
- Use standard APIs and existing repo helpers before adding custom logic.
- Reuse existing patterns. Write as little new code as the change needs.
- Delete before adding when existing code can be simplified.
- For Admin UI, use Frappe UI and the Espresso design system by default.
- Always add or update tests for behavior changes, and make sure they pass.
- Build the minimum working change, then iterate.
- Keep comments and docstrings terse. Explain only what the code does not already make obvious.
- Put detailed change explanation in commit messages or docs, not inline comments.
- Keep one owner for state that can drift out of sync.
- Keep state scoped. Do not let temporary state leak across object or module boundaries.
- Fail loudly near the bug. Do not hide corrupt or partial state behind broad fallbacks.
- Retry only operations that are safe to repeat.
- For a no-argument method that computes and returns one noun-like value, use `@property`, such as `nginx_version`.
- For methods with arguments or multi-step work, prefer `get_<what_it_returns>()`, such as `get_commit_sha()`.
- Default to public methods. Use a leading underscore only for raw parsing, security-sensitive validation, OS plumbing, or genuinely internal details callers should not reach for.
- Do not make a method private just because it currently has one caller.
- Do not split code into more helpers than necessary. A single-use one-liner usually reads better inline.
- Name boolean-returning properties and methods with `is_` or `has_`, such as `is_workload_running` or `has_passwordless_sudo`.

## Working Rules

- Do not touch unrelated dirty files.
- Do not delete data directories.
- Use `apply_patch` for manual edits.
- Run targeted tests for narrow behavior changes and `uv run pytest` before committing broad refactors.
- For bug fixes, identify the root cause before attempting a fix.
- Use `@faliure.reports_failure` decorator wherever things are async and a faliure reporting is necessary (check some existing examples to get a feel).
- Don't ignore_permissions, `frappe.get_all` in a whitelisted (`@frappe.whitelist`) function.
- Update the relevant `Readme.md` when changing the relevant code behaviour.

## Docs

Keep docs concise and current. Human readers should find the workflow quickly. LLMs should find the source of truth, object boundaries, and safe edit locations without scanning long prose.