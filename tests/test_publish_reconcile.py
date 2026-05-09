"""Tests for publish_reconcile.

Covers the HSP-004 drift scenario: a case shipped via a manual path that
bypassed ``--mode publish --execute --allow-publish``, so the yaml never
got the canonical publish_state writeback. The reconciler must verify the
real GitHub repo and patch the yaml fields HSP-002/003 already had.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from agentflow_pipeline.publish_reconcile import (
    gh_repo_view,
    reconcile_publish,
    summarize_reconcile,
)


class _FakeResult:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_view_payload(
    *,
    name: str = "stable-depeg-radar",
    owner: str = "witness1993x",
    visibility: str = "PUBLIC",
    branch: str = "main",
    spdx: str = "MIT",
    topics: tuple[str, ...] = ("python", "chainstream", "stablecoin"),
    pushed_at: str = "2026-05-02T06:59:42Z",
) -> str:
    return json.dumps(
        {
            "name": name,
            "owner": {"login": owner},
            "description": "demo",
            "visibility": visibility,
            "createdAt": "2026-05-02T06:58:21Z",
            "pushedAt": pushed_at,
            "defaultBranchRef": {"name": branch},
            "licenseInfo": {"spdxId": spdx, "name": "MIT License"},
            "repositoryTopics": [{"name": t} for t in topics],
        }
    )


def _make_runner(payload: str | None, *, returncode: int = 0):
    def _runner(cmd, cwd=None):  # noqa: ARG001
        if payload is None:
            return _FakeResult(returncode=returncode, stdout="", stderr="not found")
        return _FakeResult(returncode=returncode, stdout=payload)
    return _runner


def _drifted_config() -> dict[str, Any]:
    """Mimic HSP-004 yaml shape just before reconcile."""
    return {
        "meta": {"hotspot_id": "HSP-004"},
        "decision": {"final_status": "probe", "next_review_date": "2026-05-09"},
        "execution_state": {
            "publish": {"publish_status": "not_started"},
            "publish_readiness": {"status": "in_progress"},
        },
        "gate_5_publish_decision": {"verdict": "hold", "score": 0},
        "repo_plan": {"github_owner": "witness1993x", "repo_name": "stable-depeg-radar"},
    }


def test_reconcile_dry_run_returns_patch_preview() -> None:
    cfg = _drifted_config()
    snapshot = deepcopy(cfg)
    runner = _make_runner(_gh_view_payload())

    report = reconcile_publish(cfg, run_command=runner, dry_run=True)

    assert report["status"] == "ready_to_patch"
    assert report["repo_ref"] == "witness1993x/stable-depeg-radar"
    assert "patch_preview" in report
    assert report["patch_preview"]["publish_state"]["repo_ref"] == "witness1993x/stable-depeg-radar"
    # dry_run must not mutate.
    assert cfg == snapshot


def test_reconcile_execute_patches_canonical_fields() -> None:
    cfg = _drifted_config()
    runner = _make_runner(_gh_view_payload())

    report = reconcile_publish(cfg, run_command=runner, dry_run=False)

    assert report["status"] == "patched"
    publish = cfg["execution_state"]["publish"]
    assert publish["publish_status"] == "passed"
    assert publish["repo_ref"] == "witness1993x/stable-depeg-radar"
    assert publish["repo_url"] == "https://github.com/witness1993x/stable-depeg-radar"
    assert publish["visibility"] == "public"
    assert publish["default_branch"] == "main"
    assert publish["license"] == "MIT"
    assert publish["topics"] == ["python", "chainstream", "stablecoin"]

    decision = cfg["decision"]
    assert decision["final_status"] == "publish"
    assert decision["next_action"] == "monitor repository adoption"

    assert cfg["gate_5_publish_decision"]["verdict"] == "pass"
    assert cfg["gate_5_publish_decision"]["score"] == 4
    assert cfg["execution_state"]["publish_readiness"]["status"] == "published"

    assert "execution_state.publish" in report["patched_keys"]


def test_reconcile_idempotent_when_already_published() -> None:
    cfg = _drifted_config()
    cfg["decision"]["final_status"] = "publish"
    cfg["execution_state"]["publish"]["publish_status"] = "passed"
    snapshot = deepcopy(cfg)
    # gh should NOT be called when already reconciled.
    def _explode(cmd, cwd=None):  # noqa: ARG001
        raise AssertionError("gh repo view must not be called when already reconciled")

    report = reconcile_publish(cfg, run_command=_explode, dry_run=False)

    assert report["status"] == "already_reconciled"
    assert cfg == snapshot


def test_reconcile_bails_when_repo_plan_empty() -> None:
    cfg = _drifted_config()
    cfg["repo_plan"] = {"github_owner": "", "repo_name": ""}

    report = reconcile_publish(cfg, run_command=_make_runner(""), dry_run=False)

    assert report["status"] == "no_repo_plan"
    assert cfg["decision"]["final_status"] == "probe"  # untouched


def test_reconcile_bails_when_repo_plan_missing() -> None:
    cfg = _drifted_config()
    del cfg["repo_plan"]

    report = reconcile_publish(cfg, run_command=_make_runner(""), dry_run=False)

    assert report["status"] == "no_repo_plan"


def test_reconcile_bails_when_gh_returns_nonzero() -> None:
    cfg = _drifted_config()
    runner = _make_runner(None, returncode=1)

    report = reconcile_publish(cfg, run_command=runner, dry_run=False)

    assert report["status"] == "repo_not_found"
    assert cfg["execution_state"]["publish"]["publish_status"] == "not_started"
    assert cfg["decision"]["final_status"] == "probe"


def test_reconcile_handles_bad_json_from_gh() -> None:
    cfg = _drifted_config()
    runner = _make_runner("not-json")

    report = reconcile_publish(cfg, run_command=runner, dry_run=False)

    assert report["status"] == "repo_not_found"


def test_gh_repo_view_handles_missing_gh_binary() -> None:
    def _no_gh(cmd, cwd=None):  # noqa: ARG001
        raise FileNotFoundError("gh not on PATH")

    assert gh_repo_view("a/b", run_command=_no_gh) is None


def test_summarize_reconcile_phrases() -> None:
    assert "already reconciled" in summarize_reconcile(
        {"status": "already_reconciled", "repo_ref": "x/y"}
    )
    assert "missing" in summarize_reconcile({"status": "no_repo_plan"})
    assert "not found" in summarize_reconcile({"status": "repo_not_found", "repo_ref": "x/y"})
    assert "patched" in summarize_reconcile(
        {"status": "patched", "repo_ref": "x/y", "patched_keys": ["a", "b"]}
    )


def test_reconcile_topics_string_form_also_accepted() -> None:
    """Some gh client versions return topics as plain strings; tolerate both."""
    cfg = _drifted_config()
    payload = json.dumps(
        {
            "name": "demo",
            "owner": {"login": "x"},
            "visibility": "PUBLIC",
            "createdAt": "",
            "pushedAt": "",
            "defaultBranchRef": {"name": "main"},
            "licenseInfo": None,
            "repositoryTopics": ["alpha", "beta"],
        }
    )
    report = reconcile_publish(cfg, run_command=_make_runner(payload), dry_run=False)
    assert report["status"] == "patched"
    assert cfg["execution_state"]["publish"]["topics"] == ["alpha", "beta"]


def test_reconcile_does_not_overwrite_existing_score_when_already_set() -> None:
    cfg = _drifted_config()
    cfg["gate_5_publish_decision"]["score"] = 3  # operator-set
    runner = _make_runner(_gh_view_payload())
    reconcile_publish(cfg, run_command=runner, dry_run=False)
    assert cfg["gate_5_publish_decision"]["score"] == 3
    assert cfg["gate_5_publish_decision"]["verdict"] == "pass"
