"""Tests for monitoring_setup.seed_credits_check_workflow secret-aware skip.

The credits-check cron without a CHAINSTREAM_API_KEY secret would fail every
day with no in-workflow recovery path, polluting repo-admin email and the
Actions health signal. This test pins the fail-closed behavior.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentflow_pipeline.monitoring_setup import (
    _CREDITS_WORKFLOW_REL,
    run_monitoring_setup,
    seed_credits_check_workflow,
)


def _workflow_path(workspace: Path) -> Path:
    return workspace / _CREDITS_WORKFLOW_REL


def test_seed_credits_writes_when_secret_planned(tmp_path: Path) -> None:
    report = seed_credits_check_workflow(
        tmp_path,
        secrets_planned={"CHAINSTREAM_API_KEY"},
    )
    assert report["written"] is True
    assert report["skipped"] is False
    assert "skip_reason" not in report
    assert _workflow_path(tmp_path).exists()


def test_seed_credits_skips_when_secret_missing(tmp_path: Path) -> None:
    report = seed_credits_check_workflow(
        tmp_path,
        secrets_planned=set(),
    )
    assert report["written"] is False
    assert report["skipped"] is True
    assert report["skip_reason"] == "missing_secret_CHAINSTREAM_API_KEY"
    assert not _workflow_path(tmp_path).exists()


def test_seed_credits_skips_when_unrelated_secrets_only(tmp_path: Path) -> None:
    report = seed_credits_check_workflow(
        tmp_path,
        secrets_planned={"PAGERDUTY_INTEGRATION_KEY", "GRAFANA_TOKEN"},
    )
    assert report["skipped"] is True
    assert report["skip_reason"] == "missing_secret_CHAINSTREAM_API_KEY"
    assert not _workflow_path(tmp_path).exists()


def test_seed_credits_legacy_no_planned_arg_keeps_writing(tmp_path: Path) -> None:
    """Backward compat: callers that don't pass secrets_planned keep the old
    always-seed behavior."""
    report = seed_credits_check_workflow(tmp_path)
    assert report["written"] is True
    assert report["skipped"] is False
    assert _workflow_path(tmp_path).exists()


def test_seed_credits_idempotent_when_already_present(tmp_path: Path) -> None:
    target = _workflow_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# pre-existing\n", encoding="utf-8")
    report = seed_credits_check_workflow(
        tmp_path,
        secrets_planned={"CHAINSTREAM_API_KEY"},
    )
    assert report["written"] is False
    assert report["skipped"] is True
    assert target.read_text(encoding="utf-8") == "# pre-existing\n"


def _args(*, secret_pairs: list[str]) -> argparse.Namespace:
    return argparse.Namespace(
        apply_monitoring=False,
        monitoring_secret_from_env=secret_pairs,
        monitoring_protect_branch="main",
        monitoring_required_checks="build,test",
        monitoring_required_reviews=1,
    )


def test_run_monitoring_setup_skips_credits_when_no_secret(
    tmp_path: Path,
) -> None:
    """Integration: end-to-end run_monitoring_setup without CHAINSTREAM_API_KEY
    in env must not seed the cron, even if the workspace exists."""

    def stub_run(cmd, cwd=None):  # noqa: ARG001
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    report = run_monitoring_setup(
        workspace=tmp_path,
        config={"meta": {"hotspot_id": "HSP-TEST"}},
        repo_ref="example/demo",
        args=_args(secret_pairs=[]),
        run_command=stub_run,
        env={},
    )
    cw = report["credits_workflow"]
    assert cw["skipped"] is True
    assert cw.get("skip_reason") == "missing_secret_CHAINSTREAM_API_KEY"
    assert not _workflow_path(tmp_path).exists()


def test_run_monitoring_setup_seeds_credits_when_secret_present(
    tmp_path: Path,
) -> None:
    def stub_run(cmd, cwd=None):  # noqa: ARG001
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    report = run_monitoring_setup(
        workspace=tmp_path,
        config={"meta": {"hotspot_id": "HSP-TEST"}},
        repo_ref="example/demo",
        args=_args(secret_pairs=["CHAINSTREAM_API_KEY=CHAINSTREAM_API_KEY"]),
        run_command=stub_run,
        env={"CHAINSTREAM_API_KEY": "fake-test-value-not-real"},
    )
    cw = report["credits_workflow"]
    assert cw["written"] is True
    assert cw["skipped"] is False
    assert _workflow_path(tmp_path).exists()
    # Sanity: secret value never echoed in any sub-report (defense-in-depth).
    flat = repr(report)
    assert "fake-test-value-not-real" not in flat
