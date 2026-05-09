"""Real post-publish monitoring automation.

`post_publish.py` only renders templates into a workspace. This module performs
the *real* one-shot monitoring setup against GitHub for a freshly published
repo:

1. ``apply_repo_secrets``         - ``gh secret set`` per (name -> env var)
2. ``enable_branch_protection``   - ``gh api -X PUT .../branches/<b>/protection``
3. ``enable_security_features``   - dependabot security updates + vuln alerts
4. ``seed_credits_check_workflow``- writes ``.github/workflows/chainstream-credits.yml``
5. ``seed_runbook``               - writes ``RUNBOOK.md`` with kill switch / contacts

Every step is **fail-closed and dry-run by default**. Real ``gh`` calls only
happen when the operator explicitly passes ``--apply-monitoring``. Step
failures are caught and recorded in the report under ``errors``; nothing
escapes upward.

**Secret hygiene**: secret values are only ever sent to ``gh`` via the
``--body`` flag; they are *never* logged, returned, echoed, or even kept in
the report dict. Only secret names appear in any output.

Integration patch
-----------------
Add the import near the other top-level imports in ``run_pipeline.py``::

    from monitoring_setup import (
        register_monitoring_args,
        run_monitoring_setup,
    )

Inside ``parse_args()`` (next to ``register_auto_publish_args(parser)``)::

    register_monitoring_args(parser)

Inside ``_run_probe_or_publish_branch``, *after* ``apply_post_publish_templates``
runs and *before* ``writeback_probe`` is called::

    monitoring_result = run_monitoring_setup(
        workspace,
        config,
        repo_ref=repo_ref,
        args=args,
        run_command=run_command,
    )
    publish_state["monitoring"] = monitoring_result
    print_section("Post-Publish Monitoring")
    print(monitoring_result.get("summary", "monitoring: (no summary)"))

The call uses ``dry_run = not args.apply_monitoring``: omitting the flag keeps
the run safe (plans only). The result lands on
``execution_state.publish.monitoring`` so writeback / memo can reference it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

# Type alias: the run_command callable signature matches run_pipeline.run_command.
RunCommand = Callable[..., Any]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ok(result: Any) -> bool:
    """Best-effort success check that works for subprocess.CompletedProcess
    and any duck-typed stand-in (returncode==0 means success)."""
    code = getattr(result, "returncode", None)
    if code is None and isinstance(result, dict):
        code = result.get("returncode")
    return code == 0


def _stderr(result: Any) -> str:
    err = getattr(result, "stderr", "")
    if not err and isinstance(result, dict):
        err = result.get("stderr", "")
    return (err or "").strip()


def _safe_repo_ref(repo_ref: str) -> bool:
    """A real ``owner/name`` slug. Empty / malformed -> fail closed."""
    if not isinstance(repo_ref, str):
        return False
    repo_ref = repo_ref.strip()
    if "/" not in repo_ref:
        return False
    owner, _, name = repo_ref.partition("/")
    return bool(owner) and bool(name) and "/" not in name


def _redact(name: str) -> str:
    """Return only the secret name; never the value. Used in logs."""
    return f"<redacted:{name}>"


# ---------------------------------------------------------------------------
# 1. Repo secrets
# ---------------------------------------------------------------------------


def apply_repo_secrets(
    repo_ref: str,
    secrets: dict[str, str],
    *,
    dry_run: bool,
    run_command: RunCommand,
) -> dict:
    """Set GitHub Actions repo secrets via ``gh secret set``.

    Parameters
    ----------
    repo_ref:
        ``owner/name`` slug.
    secrets:
        Mapping of secret name -> secret value. Values are never logged or
        returned. An empty/whitespace value is treated as "missing" and the
        secret is skipped (recorded under ``errors``).
    dry_run:
        When True, only the *names* are listed in ``dry_run_planned`` and no
        ``gh`` call is made.
    run_command:
        Injected runner; expected to behave like ``subprocess.run`` and
        return an object with ``returncode`` / ``stderr``.

    Returns ``{"set": [...], "dry_run_planned": [...], "errors": [...]}``.
    """
    report: dict[str, list] = {"set": [], "dry_run_planned": [], "errors": []}

    if not _safe_repo_ref(repo_ref):
        report["errors"].append({"step": "secrets", "reason": "invalid repo_ref"})
        return report

    if not isinstance(secrets, dict) or not secrets:
        return report

    for name, value in secrets.items():
        if not isinstance(name, str) or not name.strip():
            report["errors"].append({"step": "secrets", "reason": "empty secret name"})
            continue
        name = name.strip()

        # Empty value => cannot set; record by name only, never value.
        if not isinstance(value, str) or not value:
            report["errors"].append(
                {"step": "secrets", "name": name, "reason": "empty value"}
            )
            continue

        if dry_run:
            # Names only. Never the value.
            report["dry_run_planned"].append(name)
            continue

        try:
            result = run_command(
                ["gh", "secret", "set", name, "--repo", repo_ref, "--body", value]
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed
            report["errors"].append(
                {"step": "secrets", "name": name, "reason": f"exception: {type(exc).__name__}"}
            )
            continue

        if _ok(result):
            report["set"].append(name)
        else:
            # stderr from gh may include "could not resolve to a Repository";
            # we deliberately strip any trace that might include the value.
            stderr = _stderr(result)
            # Defense-in-depth: scrub the value in case gh ever echoed it.
            if value and value in stderr:
                stderr = stderr.replace(value, _redact(name))
            report["errors"].append(
                {"step": "secrets", "name": name, "reason": stderr or "gh failed"}
            )

    return report


# ---------------------------------------------------------------------------
# 2. Branch protection
# ---------------------------------------------------------------------------


def enable_branch_protection(
    repo_ref: str,
    branch: str,
    *,
    required_status_checks: list[str],
    required_review_count: int,
    dry_run: bool,
    run_command: RunCommand,
) -> dict:
    """Enable branch protection via ``gh api -X PUT``.

    The PUT payload is composed inline: required_status_checks (strict +
    contexts), enforce_admins, required_pull_request_reviews, no
    restrictions, no linear-history requirement.
    """
    report: dict[str, Any] = {
        "branch": branch,
        "applied": False,
        "dry_run": dry_run,
        "planned_contexts": list(required_status_checks or []),
        "planned_review_count": int(required_review_count),
        "errors": [],
    }

    if not _safe_repo_ref(repo_ref):
        report["errors"].append({"step": "branch_protection", "reason": "invalid repo_ref"})
        return report
    if not isinstance(branch, str) or not branch.strip():
        report["errors"].append({"step": "branch_protection", "reason": "empty branch"})
        return report

    contexts = [c for c in (required_status_checks or []) if isinstance(c, str) and c.strip()]
    review_count = max(0, int(required_review_count))

    # gh api accepts repeated -F field=value pairs; for nested fields we use -f
    # for strings and --raw-field for JSON. The simplest portable approach is
    # to feed a single JSON document via --input -.
    payload = {
        "required_status_checks": {
            "strict": True,
            "contexts": contexts,
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "required_approving_review_count": review_count,
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
        },
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }

    endpoint = f"repos/{repo_ref}/branches/{branch}/protection"
    cmd = ["gh", "api", "-X", "PUT", endpoint, "--input", "-"]
    report["planned_command"] = cmd
    report["planned_payload"] = payload

    if dry_run:
        return report

    try:
        # The injected run_command may or may not support stdin; we try
        # input= first, fall back to writing payload to a temp file.
        try:
            result = run_command(cmd, input=json.dumps(payload))
        except TypeError:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as tmp:
                tmp.write(json.dumps(payload))
                tmp_path = tmp.name
            try:
                result = run_command(
                    ["gh", "api", "-X", "PUT", endpoint, "--input", tmp_path]
                )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(
            {"step": "branch_protection", "reason": f"exception: {type(exc).__name__}"}
        )
        return report

    if _ok(result):
        report["applied"] = True
    else:
        report["errors"].append(
            {"step": "branch_protection", "reason": _stderr(result) or "gh api failed"}
        )
    return report


# ---------------------------------------------------------------------------
# 3. Security features
# ---------------------------------------------------------------------------


def enable_security_features(
    repo_ref: str,
    *,
    dry_run: bool,
    run_command: RunCommand,
) -> dict:
    """Enable vulnerability alerts + automated security fixes (dependabot)."""
    report: dict[str, Any] = {
        "vulnerability_alerts": False,
        "automated_security_fixes": False,
        "dry_run": dry_run,
        "planned": [],
        "errors": [],
    }

    if not _safe_repo_ref(repo_ref):
        report["errors"].append({"step": "security", "reason": "invalid repo_ref"})
        return report

    endpoints = [
        ("vulnerability_alerts", f"repos/{repo_ref}/vulnerability-alerts"),
        ("automated_security_fixes", f"repos/{repo_ref}/automated-security-fixes"),
    ]

    for key, endpoint in endpoints:
        cmd = ["gh", "api", "-X", "PUT", endpoint]
        report["planned"].append(cmd)
        if dry_run:
            continue
        try:
            result = run_command(cmd)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(
                {"step": "security", "endpoint": endpoint, "reason": f"exception: {type(exc).__name__}"}
            )
            continue
        if _ok(result):
            report[key] = True
        else:
            report["errors"].append(
                {"step": "security", "endpoint": endpoint, "reason": _stderr(result) or "gh api failed"}
            )

    return report


# ---------------------------------------------------------------------------
# 4. Credits-check workflow
# ---------------------------------------------------------------------------


_CREDITS_WORKFLOW_REL = ".github/workflows/chainstream-credits.yml"

_CREDITS_WORKFLOW_BODY = """\
# Auto-generated by monitoring_setup.seed_credits_check_workflow.
# Daily ChainStream credits monitor. Fails the job (and notifies repo
# admins via GitHub's normal workflow-failure email) when credits drop
# below CREDITS_MIN_THRESHOLD.
name: chainstream-credits

on:
  schedule:
    # 09:00 UTC daily.
    - cron: "0 9 * * *"
  workflow_dispatch:

jobs:
  check-credits:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    env:
      CHAINSTREAM_API_KEY: ${{ secrets.CHAINSTREAM_API_KEY }}
      CREDITS_MIN_THRESHOLD: "1000"
    steps:
      - name: Query ChainStream credits
        run: |
          set -euo pipefail
          if [ -z "${CHAINSTREAM_API_KEY:-}" ]; then
            echo "::error::CHAINSTREAM_API_KEY secret is not configured"
            exit 1
          fi
          response=$(curl -fsS \\
            -H "Authorization: Bearer ${CHAINSTREAM_API_KEY}" \\
            -H "Accept: application/json" \\
            https://api.chainstream.io/v1/account/credits)
          echo "credits response: ${response}"
          credits=$(printf '%s' "${response}" | python -c \\
            'import json,sys; d=json.load(sys.stdin); print(int(d.get("credits",0)))')
          echo "credits=${credits}"
          if [ "${credits}" -lt "${CREDITS_MIN_THRESHOLD}" ]; then
            echo "::error::ChainStream credits ${credits} below threshold ${CREDITS_MIN_THRESHOLD}"
            exit 1
          fi
"""


def seed_credits_check_workflow(
    workspace: Path,
    *,
    secrets_planned: set[str] | None = None,
) -> dict:
    """Write the daily credits-check workflow. Skip if it already exists.

    ``secrets_planned``: when provided, must contain ``CHAINSTREAM_API_KEY``
    or this step is skipped with ``skip_reason="missing_secret_CHAINSTREAM_API_KEY"``.
    Without that secret the cron would fail every day with no way to recover,
    polluting repo-admin email and the Actions health signal. ``None`` keeps
    legacy behavior for older callers.
    """
    workspace = Path(workspace)
    target = workspace / _CREDITS_WORKFLOW_REL
    report: dict[str, Any] = {"path": str(target.relative_to(workspace)), "written": False, "skipped": False, "errors": []}

    if secrets_planned is not None and "CHAINSTREAM_API_KEY" not in secrets_planned:
        report["skipped"] = True
        report["skip_reason"] = "missing_secret_CHAINSTREAM_API_KEY"
        return report

    try:
        if not workspace.exists():
            report["errors"].append({"step": "credits_workflow", "reason": "workspace missing"})
            return report
        if target.exists():
            report["skipped"] = True
            return report
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_CREDITS_WORKFLOW_BODY, encoding="utf-8")
        report["written"] = True
    except Exception as exc:  # noqa: BLE001
        report["errors"].append({"step": "credits_workflow", "reason": f"exception: {type(exc).__name__}: {exc}"})

    return report


# ---------------------------------------------------------------------------
# 5. RUNBOOK.md
# ---------------------------------------------------------------------------


_RUNBOOK_REL = "RUNBOOK.md"


def _runbook_body(*, repo_ref: str, owner_name: str, owner_contact: str, threshold: str) -> str:
    return f"""\
# RUNBOOK

> Operational playbook for `{repo_ref or '<owner/repo>'}`.
> Auto-generated by `monitoring_setup.seed_runbook`. Edit freely; this file is
> only created if it does not already exist.

## Monitoring

- GitHub Actions: <https://github.com/{repo_ref or '<owner/repo>'}/actions>
- ChainStream credits check: `.github/workflows/chainstream-credits.yml`
  (daily; fails when credits < `{threshold}`).
- See also: `MONITORING.md` for dashboards, log queries, and SLOs.

## Emergency Contacts

- Primary owner: **{owner_name or '<unset>'}**{f" ({owner_contact})" if owner_contact else ""}
- Escalation: open a P1 issue with label `incident` on this repo.

## Kill Switch

If the deployment is causing production damage and rollback alone is not
enough, flip the repo to private to immediately stop downstream consumers
that pull tagged releases or `main`:

```sh
gh repo edit {repo_ref or '<owner/repo>'} --visibility private
```

To restore:

```sh
gh repo edit {repo_ref or '<owner/repo>'} --visibility public
```

## Credits / Quota

- Threshold: `{threshold}` credits.
- Secret: `CHAINSTREAM_API_KEY` (set via `gh secret set`).
- On credits-check failure: rotate keys or top up via the ChainStream console,
  then re-run the workflow with **Run workflow** on the Actions tab.

## Branch Protection

`main` is protected; required status checks and review count are enforced via
`monitoring_setup.enable_branch_protection`. Update via:

```sh
gh api -X PUT repos/{repo_ref or '<owner/repo>'}/branches/main/protection --input <payload>.json
```
"""


def seed_runbook(workspace: Path, config: dict, repo_ref: str) -> dict:
    """Write `RUNBOOK.md` if missing."""
    workspace = Path(workspace)
    target = workspace / _RUNBOOK_REL
    report: dict[str, Any] = {"path": _RUNBOOK_REL, "written": False, "skipped": False, "errors": []}

    try:
        if not workspace.exists():
            report["errors"].append({"step": "runbook", "reason": "workspace missing"})
            return report
        if target.exists():
            report["skipped"] = True
            return report

        meta = (config or {}).get("meta") or {}
        owner_block = meta.get("owner") or {}
        if isinstance(owner_block, dict):
            owner_name = str(owner_block.get("name") or owner_block.get("handle") or "").strip()
            owner_contact = str(owner_block.get("contact") or owner_block.get("email") or "").strip()
        elif isinstance(owner_block, str):
            owner_name, owner_contact = owner_block.strip(), ""
        else:
            owner_name, owner_contact = "", ""

        monitoring_block = (config or {}).get("monitoring") or {}
        threshold = str(monitoring_block.get("credits_threshold") or "1000")

        body = _runbook_body(
            repo_ref=repo_ref,
            owner_name=owner_name,
            owner_contact=owner_contact,
            threshold=threshold,
        )
        target.write_text(body, encoding="utf-8")
        report["written"] = True
    except Exception as exc:  # noqa: BLE001
        report["errors"].append({"step": "runbook", "reason": f"exception: {type(exc).__name__}: {exc}"})

    return report


# ---------------------------------------------------------------------------
# CLI registration & glue
# ---------------------------------------------------------------------------


def register_monitoring_args(parser: argparse.ArgumentParser) -> None:
    """Register monitoring-related CLI flags on ``parser``.

    Defaults are intentionally passive: without ``--apply-monitoring`` the
    pipeline only plans (dry run). Secret values are read from the calling
    process's environment, never the command line.
    """
    parser.add_argument(
        "--apply-monitoring",
        action="store_true",
        default=False,
        help=(
            "Actually apply post-publish monitoring (gh secret set, branch "
            "protection, dependabot/security alerts). Without this flag, "
            "monitoring runs in dry-run mode and only files RUNBOOK.md / "
            "the credits workflow into the workspace."
        ),
    )
    parser.add_argument(
        "--monitoring-secret-from-env",
        action="append",
        default=[],
        metavar="NAME=ENV_VAR",
        help=(
            "Map a GitHub Actions secret to an environment variable on this "
            "host. Repeatable. Example: "
            "--monitoring-secret-from-env CHAINSTREAM_API_KEY=CHAINSTREAM_API_KEY"
        ),
    )
    parser.add_argument(
        "--monitoring-protect-branch",
        default="main",
        help="Branch to apply protection to (default: main).",
    )
    parser.add_argument(
        "--monitoring-required-checks",
        default="build,test",
        help="Comma-separated required status check contexts (default: 'build,test').",
    )
    parser.add_argument(
        "--monitoring-required-reviews",
        type=int,
        default=1,
        help="Number of required PR approving reviews (default: 1).",
    )


def _parse_secret_pairs(pairs: Iterable[str], env: dict[str, str]) -> tuple[dict[str, str], list[dict]]:
    """Resolve ``NAME=ENV_VAR`` pairs from ``env``. Values are returned but
    must be treated as confidential by callers."""
    secrets: dict[str, str] = {}
    errors: list[dict] = []
    for raw in pairs or []:
        if not isinstance(raw, str) or "=" not in raw:
            errors.append({"step": "secrets", "reason": f"bad pair: {raw!r}"})
            continue
        name, _, env_var = raw.partition("=")
        name = name.strip()
        env_var = env_var.strip()
        if not name or not env_var:
            errors.append({"step": "secrets", "reason": f"bad pair: {raw!r}"})
            continue
        value = env.get(env_var)
        if value is None or value == "":
            # Recorded by name only.
            errors.append({"step": "secrets", "name": name, "reason": f"env var {env_var} not set"})
            continue
        secrets[name] = value
    return secrets, errors


def monitoring_args_to_kwargs(args: argparse.Namespace) -> dict:
    """Translate parsed args into a kwargs dict for `run_monitoring_setup`."""
    checks_raw = getattr(args, "monitoring_required_checks", "") or ""
    contexts = [c.strip() for c in checks_raw.split(",") if c.strip()]
    return {
        "apply": bool(getattr(args, "apply_monitoring", False)),
        "secret_pairs": list(getattr(args, "monitoring_secret_from_env", []) or []),
        "branch": getattr(args, "monitoring_protect_branch", "main") or "main",
        "required_status_checks": contexts,
        "required_review_count": int(getattr(args, "monitoring_required_reviews", 1) or 0),
    }


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run_monitoring_setup(
    workspace: Path,
    config: dict,
    repo_ref: str,
    args: argparse.Namespace,
    *,
    run_command: RunCommand,
    env: dict[str, str] | None = None,
) -> dict:
    """One-shot post-publish monitoring driver.

    Composes all five steps. **Fail-closed**: if ``repo_ref`` is missing or
    malformed, every gh-touching step is forced to dry-run. Step-level
    failures are caught and aggregated under ``errors``; this function never
    raises.
    """
    env = env if env is not None else os.environ.copy()
    kwargs = monitoring_args_to_kwargs(args)
    apply = kwargs["apply"]

    repo_ok = _safe_repo_ref(repo_ref)
    # Fail-closed: any structural issue forces a dry-run for the gh steps.
    effective_dry_run = (not apply) or (not repo_ok)

    report: dict[str, Any] = {
        "repo_ref": repo_ref,
        "apply_requested": apply,
        "dry_run": effective_dry_run,
        "errors": [],
    }
    if not repo_ok:
        report["errors"].append(
            {"step": "monitoring", "reason": "repo_ref missing or malformed; forcing dry-run"}
        )

    # 1. Resolve secrets (names only logged; values held only in memory).
    secrets, secret_errors = _parse_secret_pairs(kwargs["secret_pairs"], env)
    if secret_errors:
        report["errors"].extend(secret_errors)
    try:
        report["secrets"] = apply_repo_secrets(
            repo_ref,
            secrets,
            dry_run=effective_dry_run,
            run_command=run_command,
        )
    except Exception as exc:  # noqa: BLE001 - belt + suspenders
        report["secrets"] = {"set": [], "dry_run_planned": [], "errors": [
            {"step": "secrets", "reason": f"exception: {type(exc).__name__}"}
        ]}

    # 2. Branch protection.
    try:
        report["branch_protection"] = enable_branch_protection(
            repo_ref,
            kwargs["branch"],
            required_status_checks=kwargs["required_status_checks"],
            required_review_count=kwargs["required_review_count"],
            dry_run=effective_dry_run,
            run_command=run_command,
        )
    except Exception as exc:  # noqa: BLE001
        report["branch_protection"] = {"applied": False, "errors": [
            {"step": "branch_protection", "reason": f"exception: {type(exc).__name__}"}
        ]}

    # 3. Security features.
    try:
        report["security"] = enable_security_features(
            repo_ref,
            dry_run=effective_dry_run,
            run_command=run_command,
        )
    except Exception as exc:  # noqa: BLE001
        report["security"] = {"errors": [
            {"step": "security", "reason": f"exception: {type(exc).__name__}"}
        ]}

    # 4. Credits-check workflow (workspace-only). Skipped when CHAINSTREAM_API_KEY
    # is not in the planned secrets — seeding the cron without the secret causes
    # daily fail-emails with no way for the workflow itself to recover.
    try:
        report["credits_workflow"] = seed_credits_check_workflow(
            workspace,
            secrets_planned=set(secrets.keys()),
        )
    except Exception as exc:  # noqa: BLE001
        report["credits_workflow"] = {"errors": [
            {"step": "credits_workflow", "reason": f"exception: {type(exc).__name__}"}
        ]}

    # 5. RUNBOOK.md (workspace-only).
    try:
        report["runbook"] = seed_runbook(workspace, config, repo_ref)
    except Exception as exc:  # noqa: BLE001
        report["runbook"] = {"errors": [
            {"step": "runbook", "reason": f"exception: {type(exc).__name__}"}
        ]}

    report["summary"] = _summarize(report)
    return report


def _summarize(report: dict) -> str:
    secrets = report.get("secrets") or {}
    bp = report.get("branch_protection") or {}
    sec = report.get("security") or {}
    cw = report.get("credits_workflow") or {}
    rb = report.get("runbook") or {}
    mode = "dry-run" if report.get("dry_run") else "execute"
    lines = [
        f"monitoring [{mode}] for {report.get('repo_ref') or '<no repo_ref>'}",
        (
            f"  secrets: set={len(secrets.get('set', []))} "
            f"planned={len(secrets.get('dry_run_planned', []))} "
            f"errors={len(secrets.get('errors', []))}"
        ),
        (
            f"  branch_protection: applied={bp.get('applied', False)} "
            f"branch={bp.get('branch')} errors={len(bp.get('errors', []))}"
        ),
        (
            f"  security: vuln_alerts={sec.get('vulnerability_alerts', False)} "
            f"auto_fixes={sec.get('automated_security_fixes', False)} "
            f"errors={len(sec.get('errors', []))}"
        ),
        (
            f"  credits_workflow: written={cw.get('written', False)} "
            f"skipped={cw.get('skipped', False)}"
        ),
        f"  runbook: written={rb.get('written', False)} skipped={rb.get('skipped', False)}",
    ]
    top_errors = report.get("errors") or []
    if top_errors:
        lines.append(f"  top-level errors: {len(top_errors)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    """Smoke test: dry-run never touches gh; execute path does; secret values
    never appear in stdout or in the result dict."""
    import contextlib
    import io

    secret_value = "super-secret-DO-NOT-LEAK-9X8Y7"
    other_value = "slack-hook-url-DO-NOT-LEAK"
    env = {
        "CHAINSTREAM_API_KEY": secret_value,
        "SLACK_WEBHOOK_URL": other_value,
    }

    # Capture every command observed. Each call returns a fake completed proc.
    class FakeProc:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    calls: list[dict] = []

    def fake_run_command(cmd, cwd=None, input=None):  # noqa: A002 - shadow ok
        calls.append({"cmd": list(cmd), "input": input})
        return FakeProc(returncode=0)

    config = {
        "meta": {"owner": {"name": "Alice Example", "contact": "alice@example.com"}},
        "monitoring": {"credits_threshold": 500},
    }

    repo_ref = "alice/demo-repo"

    # Build args for both paths.
    def make_parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        register_monitoring_args(p)
        return p

    common_args = [
        "--monitoring-secret-from-env", "CHAINSTREAM_API_KEY=CHAINSTREAM_API_KEY",
        "--monitoring-secret-from-env", "SLACK_WEBHOOK_URL=SLACK_WEBHOOK_URL",
        "--monitoring-required-checks", "build,test,lint",
        "--monitoring-required-reviews", "2",
    ]

    failures: list[str] = []

    # ------------------------------------------------------------------
    # Phase A: dry-run (no --apply-monitoring)
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "ws"
        workspace.mkdir()

        args = make_parser().parse_args(common_args)
        calls.clear()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = run_monitoring_setup(
                workspace, config, repo_ref, args,
                run_command=fake_run_command, env=env,
            )
            print(result["summary"])

        stdout = buf.getvalue()
        result_blob = json.dumps(result)

        if calls:
            failures.append(f"dry-run should not call gh, but saw {len(calls)} calls")
        if not result["dry_run"]:
            failures.append("dry_run flag should be True without --apply-monitoring")
        if "CHAINSTREAM_API_KEY" not in result["secrets"]["dry_run_planned"]:
            failures.append("CHAINSTREAM_API_KEY missing from dry_run_planned")

        # Critical: the secret VALUES must never appear anywhere.
        if secret_value in stdout or other_value in stdout:
            failures.append("secret value leaked into stdout (dry-run)")
        if secret_value in result_blob or other_value in result_blob:
            failures.append("secret value leaked into result dict (dry-run)")

        # Workspace-only steps still happen even in dry-run.
        if not (workspace / ".github" / "workflows" / "chainstream-credits.yml").exists():
            failures.append("credits workflow not written in dry-run")
        if not (workspace / "RUNBOOK.md").exists():
            failures.append("RUNBOOK.md not written in dry-run")

        # Idempotency: second run skips both files.
        result2 = run_monitoring_setup(
            workspace, config, repo_ref, args,
            run_command=fake_run_command, env=env,
        )
        if not result2["credits_workflow"]["skipped"]:
            failures.append("credits workflow not skipped on rerun")
        if not result2["runbook"]["skipped"]:
            failures.append("runbook not skipped on rerun")

    # ------------------------------------------------------------------
    # Phase B: execute (--apply-monitoring) -- gh IS called.
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "ws"
        workspace.mkdir()

        args = make_parser().parse_args(["--apply-monitoring", *common_args])
        calls.clear()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = run_monitoring_setup(
                workspace, config, repo_ref, args,
                run_command=fake_run_command, env=env,
            )
            print(result["summary"])

        stdout = buf.getvalue()
        result_blob = json.dumps(result)

        if not calls:
            failures.append("execute path made zero gh calls")
        # Expect: 2 secret sets + 1 branch protection + 2 security PUTs = 5.
        gh_secret_calls = [c for c in calls if c["cmd"][:3] == ["gh", "secret", "set"]]
        gh_protection = [
            c for c in calls
            if c["cmd"][:4] == ["gh", "api", "-X", "PUT"]
            and "branches" in (c["cmd"][4] if len(c["cmd"]) > 4 else "")
        ]
        gh_vuln = [
            c for c in calls
            if c["cmd"][:4] == ["gh", "api", "-X", "PUT"]
            and (len(c["cmd"]) > 4 and c["cmd"][4].endswith("/vulnerability-alerts"))
        ]
        gh_autofix = [
            c for c in calls
            if c["cmd"][:4] == ["gh", "api", "-X", "PUT"]
            and (len(c["cmd"]) > 4 and c["cmd"][4].endswith("/automated-security-fixes"))
        ]
        if len(gh_secret_calls) != 2:
            failures.append(f"expected 2 gh secret set calls, got {len(gh_secret_calls)}")
        if len(gh_protection) != 1:
            failures.append(f"expected 1 branch protection call, got {len(gh_protection)}")
        if len(gh_vuln) != 1 or len(gh_autofix) != 1:
            failures.append("expected vuln-alerts and auto-security-fixes calls")

        # The --body argument carries the secret value into the gh command.
        # That is OK (gh consumes it) but the value MUST NOT appear in
        # stdout or in the result dict.
        if secret_value in stdout or other_value in stdout:
            failures.append("secret value leaked into stdout (execute)")
        if secret_value in result_blob or other_value in result_blob:
            failures.append("secret value leaked into result dict (execute)")

        if not result["secrets"]["set"]:
            failures.append("execute path: no secrets recorded as set")
        if "CHAINSTREAM_API_KEY" not in result["secrets"]["set"]:
            failures.append("execute path: CHAINSTREAM_API_KEY missing from set list")
        if not result["branch_protection"]["applied"]:
            failures.append("execute path: branch protection not marked applied")
        if not result["security"]["vulnerability_alerts"]:
            failures.append("execute path: vuln alerts not marked enabled")

    # ------------------------------------------------------------------
    # Phase C: fail-closed when repo_ref is missing.
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "ws"
        workspace.mkdir()
        args = make_parser().parse_args(["--apply-monitoring", *common_args])
        calls.clear()
        result = run_monitoring_setup(
            workspace, config, "", args,
            run_command=fake_run_command, env=env,
        )
        if calls:
            failures.append("fail-closed: gh called even though repo_ref empty")
        if not result["dry_run"]:
            failures.append("fail-closed: dry_run should be True with empty repo_ref")

    # ------------------------------------------------------------------
    # Phase D: gh failure is captured, never raised.
    # ------------------------------------------------------------------
    def failing_run_command(cmd, cwd=None, input=None):  # noqa: A002
        return FakeProc(returncode=1, stderr="boom")

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "ws"
        workspace.mkdir()
        args = make_parser().parse_args(["--apply-monitoring", *common_args])
        try:
            result = run_monitoring_setup(
                workspace, config, repo_ref, args,
                run_command=failing_run_command, env=env,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"failure path raised: {exc!r}")
            result = {}
        if result and not result["secrets"]["errors"]:
            failures.append("failure path: secret errors not recorded")
        if result and result.get("branch_protection", {}).get("applied"):
            failures.append("failure path: branch protection wrongly marked applied")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("monitoring_setup self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
