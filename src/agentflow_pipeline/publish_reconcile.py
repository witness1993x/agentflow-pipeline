"""Reconcile a case yaml with the real GitHub repo state.

Use-case: a case was shipped via a path that bypassed the framework's
``--mode publish --execute --allow-publish`` flow (e.g. manual ``gh repo
create`` for ad-hoc fixes), so ``update_gate_after_probe`` never wrote the
``execution_state.publish`` / ``decision.final_status`` fields. The yaml
ends up drifted from reality.

This module provides a fail-closed, idempotent reconciler:

1. Read ``repo_plan.github_owner`` + ``repo_plan.repo_name`` from the case
   yaml. Empty -> bail with ``no_repo_plan``.
2. ``gh repo view <owner>/<name> --json ...`` to verify the repo really
   exists. Non-zero / missing -> bail with ``repo_not_found``.
3. If yaml already shows ``decision.final_status=='publish'`` and
   ``execution_state.publish.publish_status=='passed'``, return
   ``already_reconciled`` (idempotent).
4. Otherwise patch the yaml in-place (or report a plan if dry_run=True):
   - ``execution_state.publish``: ``last_run_at`` / ``repo_ref`` /
     ``publish_status='passed'`` / ``summary`` (matching the canonical
     shape written by ``update_gate_after_probe`` for HSP-002/003)
   - ``decision.final_status='publish'``, ``next_action='monitor
     repository adoption'``
   - ``gate_5_publish_decision.verdict='pass'`` (and score=4 if 0)

The module never calls ``gh repo create`` or any write op against GitHub.
``gh repo view`` is the only network touch, and it's read-only.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

RunCommand = Callable[..., Any]

_GH_VIEW_FIELDS = (
    "name,owner,description,visibility,createdAt,pushedAt,"
    "defaultBranchRef,licenseInfo,repositoryTopics"
)


def _ok(result: Any) -> bool:
    code = getattr(result, "returncode", None)
    if code is None and isinstance(result, dict):
        code = result.get("returncode")
    return code == 0


def _stdout(result: Any) -> str:
    out = getattr(result, "stdout", "")
    if not out and isinstance(result, dict):
        out = result.get("stdout", "")
    return out or ""


def _default_run_command(cmd: list[str], cwd: str | None = None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_repo_view(repo_ref: str, *, run_command: RunCommand | None = None) -> dict[str, Any] | None:
    """Return parsed repo metadata or None when missing/blocked."""
    runner = run_command or _default_run_command
    cmd = ["gh", "repo", "view", repo_ref, "--json", _GH_VIEW_FIELDS]
    try:
        result = runner(cmd, cwd=None)
    except (FileNotFoundError, OSError):
        return None
    if not _ok(result):
        return None
    raw = _stdout(result).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _reconcile_patch(meta: dict[str, Any], repo_ref: str) -> dict[str, Any]:
    """Build the minimal field-level patch from gh repo metadata."""
    default_branch = ""
    branch_ref = meta.get("defaultBranchRef")
    if isinstance(branch_ref, dict):
        default_branch = str(branch_ref.get("name", "") or "")
    license_name = ""
    lic = meta.get("licenseInfo")
    if isinstance(lic, dict):
        license_name = str(lic.get("spdxId", "") or lic.get("name", "") or "")
    topics_block = meta.get("repositoryTopics")
    topics: list[str] = []
    if isinstance(topics_block, list):
        for t in topics_block:
            if isinstance(t, dict):
                name = t.get("name") or t.get("topic")
                if isinstance(name, str) and name:
                    topics.append(name)
            elif isinstance(t, str) and t:
                topics.append(t)
    return {
        "publish_state": {
            "last_run_at": _iso_now(),
            "repo_ref": repo_ref,
            "publish_status": "passed",
            "summary": f"Reconciled from gh repo view: {repo_ref}",
            "repo_url": f"https://github.com/{repo_ref}",
            "visibility": str(meta.get("visibility", "") or "").lower(),
            "default_branch": default_branch,
            "license": license_name,
            "topics": topics,
            "pushed_at": str(meta.get("pushedAt", "") or ""),
        },
    }


def reconcile_publish(
    config: dict,
    *,
    run_command: RunCommand | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Drive reconciliation. Returns a report dict; mutates ``config``
    in-place only when ``dry_run=False`` and a real patch is needed."""
    report: dict[str, Any] = {
        "status": "noop",
        "repo_ref": "",
        "patched_keys": [],
        "errors": [],
        "dry_run": dry_run,
    }

    repo_plan = config.get("repo_plan") if isinstance(config, dict) else None
    if not isinstance(repo_plan, dict):
        report["status"] = "no_repo_plan"
        report["errors"].append({"step": "repo_plan", "reason": "missing repo_plan dict"})
        return report

    owner = str(repo_plan.get("github_owner", "") or "").strip()
    name = str(repo_plan.get("repo_name", "") or "").strip()
    if not owner or not name:
        report["status"] = "no_repo_plan"
        report["errors"].append(
            {"step": "repo_plan", "reason": "github_owner or repo_name empty"}
        )
        return report

    repo_ref = f"{owner}/{name}"
    report["repo_ref"] = repo_ref

    decision = config.get("decision") or {}
    publish_state_existing = (
        config.get("execution_state", {}).get("publish", {})
        if isinstance(config.get("execution_state"), dict)
        else {}
    )
    if (
        decision.get("final_status") == "publish"
        and publish_state_existing.get("publish_status") == "passed"
    ):
        report["status"] = "already_reconciled"
        return report

    meta = gh_repo_view(repo_ref, run_command=run_command)
    if meta is None:
        report["status"] = "repo_not_found"
        report["errors"].append(
            {"step": "gh_repo_view", "reason": f"gh repo view {repo_ref} failed"}
        )
        return report

    patch = _reconcile_patch(meta, repo_ref)
    report["patch_preview"] = patch
    report["status"] = "ready_to_patch" if dry_run else "patched"

    if dry_run:
        return report

    # Mutate config in place.
    execution_state = config.setdefault("execution_state", {})
    publish_state = execution_state.setdefault("publish", {})
    publish_state.update(patch["publish_state"])

    decision_block = config.setdefault("decision", {})
    decision_block["final_status"] = "publish"
    decision_block.setdefault("summary", f"Reconciled publish state for {repo_ref}.")
    decision_block["next_action"] = "monitor repository adoption"

    gate5 = config.setdefault("gate_5_publish_decision", {})
    gate5["verdict"] = "pass"
    if int(gate5.get("score", 0) or 0) == 0:
        gate5["score"] = 4

    readiness = execution_state.setdefault("publish_readiness", {})
    readiness["status"] = "published"
    readiness["reason"] = f"Reconciled from gh repo view at {publish_state['last_run_at']}"

    report["patched_keys"] = [
        "execution_state.publish",
        "decision.final_status",
        "decision.next_action",
        "gate_5_publish_decision.verdict",
        "execution_state.publish_readiness.status",
    ]
    return report


def summarize_reconcile(report: dict[str, Any]) -> str:
    status = report.get("status", "?")
    repo_ref = report.get("repo_ref", "") or "<unknown>"
    if status == "already_reconciled":
        return f"reconcile-publish: {repo_ref} already reconciled (noop)"
    if status == "no_repo_plan":
        return "reconcile-publish: repo_plan.github_owner / repo_name missing"
    if status == "repo_not_found":
        return f"reconcile-publish: {repo_ref} not found via gh repo view"
    if status == "ready_to_patch":
        keys = report.get("patch_preview", {}).get("publish_state", {})
        n = len(keys)
        return f"reconcile-publish (dry-run): would patch {repo_ref} with {n} publish_state fields"
    if status == "patched":
        keys = ", ".join(report.get("patched_keys", []))
        return f"reconcile-publish: patched {repo_ref} -> {keys}"
    return f"reconcile-publish: {status}"
