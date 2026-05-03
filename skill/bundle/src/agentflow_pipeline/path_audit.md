# Path Audit — agentflow_pipeline

This document audits every place inside `src/agentflow_pipeline/` where a
filesystem path is computed, to keep a clean separation between paths that
**must** stay inside the installed package and paths that **must** follow the
host project's working directory (so the framework works as a pip-installed
library).

## Two categories

| Bucket | Meaning | Resolves from |
|---|---|---|
| **PACKAGE_DIR-relative** | always inside the installed package; never moves with cwd | `Path(__file__).resolve().parent` |
| **ROOT-relative** | follows the host project; must respect `--root` / `AGENTFLOW_ROOT` / `cwd` | `ROOT` (host project root) |

`ROOT` resolution order (highest → lowest):

1. `--root <path>` CLI flag (added by Agent R)
2. `AGENTFLOW_ROOT` environment variable
3. `Path.cwd()` (the directory the user ran the command from)

`--workspace-root` and `--pool-file` may further override individual paths
(they win over the `ROOT`-derived defaults).

## Inventory

### `cli.py`

| Line | Symbol | Bucket | Notes |
|---|---|---|---|
| 58 | `PACKAGE_DIR = Path(__file__).resolve().parent` | PACKAGE_DIR | Reserved for future use; not currently dereferenced. |
| 59 | `ROOT = ...` | ROOT | Reads `AGENTFLOW_ROOT` / cwd. Now also overridable via `--root`. |
| 60 | `DEFAULT_WORKSPACE_ROOT = ROOT / "workspaces"` | ROOT | Host-project local workspace. |
| 61 | `DEFAULT_POOL_FILE = ROOT / "pipeline-pool.md"` | ROOT | Host-project pool. |
| 1883 | `case_dir.relative_to(ROOT)` | ROOT | Display-only; falls back to absolute on `ValueError`. |
| 2141, 2158 | `Path(__file__).resolve()` (passed as `run_pipeline_script`) | PACKAGE_DIR | Used to re-invoke the CLI in subprocesses. Refers to **this very module**, not to a user-facing script. Correct as PACKAGE_DIR. |

### `scaffold.py`

| Line | Symbol | Bucket | Notes |
|---|---|---|---|
| 13 | `PACKAGE_DIR` | PACKAGE_DIR | |
| 14 | `TEMPLATES_DIR = PACKAGE_DIR / "templates"` | PACKAGE_DIR | Template files live inside the wheel. Must stay package-relative. |
| 15 | `ROOT` | ROOT | |
| 16 | `DEFAULT_POOL_FILE` | ROOT | |
| 263 | `--output-dir` default `ROOT / "cases"` | ROOT | Was `<framework>/cases/` historically; now correctly `<host>/cases/`. |
| 374 | `case_dir.relative_to(ROOT)` | ROOT | Display-only. |

### `post_publish.py`

| Line | Symbol | Bucket | Notes |
|---|---|---|---|
| 55–56 | `_templates_root() = Path(__file__).parent / "templates" / "post-publish"` | PACKAGE_DIR | Reads template tree from inside the package. Correct. |

### `pool_runner.py` & `pool_advancer.py`

The `Path(__file__).resolve()` references at the top of these modules sit
inside docstrings (integration notes), not in executable code. The actual
runtime callers (`cli.py` lines 2141 / 2158) pass `Path(__file__)` from
**inside `cli.py`**, which correctly resolves to the installed `cli.py`.
Both `run_pipeline_script` parameters are then handed to `subprocess.run`
as `python <run_pipeline_script> --case-dir ...`, which is fine because
`cli.py`'s `if __name__ == "__main__"` block dispatches to `_main_entry()`.

## Conclusions

* Every PACKAGE_DIR-relative usage is template-file lookup or self-reference for
  re-invocation — both legitimate.
* Every ROOT-relative usage now flows through the same `ROOT` constant, which
  honours `--root` > `AGENTFLOW_ROOT` > `cwd`.
* No "framework directory" path leaked into a runtime default.
* Existing fallback (running from inside the framework checkout) still works
  because `cwd` of that checkout matches what `ROOT` used to be hard-coded to.

## Verified by

* `tests/test_host_project_usage.py` — 5 subprocess-driven tests that prove
  `agentflow-scaffold` and `agentflow-pipeline` write into the host project,
  not into the framework directory, when invoked from outside it.
* Manual smoke test in `/tmp/host-project` showed `cases/HSP-001-...` created
  at the host root.
