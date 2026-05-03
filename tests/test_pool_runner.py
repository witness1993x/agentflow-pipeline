"""Tests for ``pool_runner`` -- pool-level parallel pipeline executor."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from agentflow_pipeline import pool_runner
from agentflow_pipeline.pool_runner import (
    find_pool_cases,
    format_pool_summary,
    pool_args_to_kwargs,
    register_pool_args,
    run_pool_parallel,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_case(
    root: Path,
    name: str,
    *,
    final_status: str | None = "watch",
    write_yaml: bool = True,
) -> Path:
    """Create a fake case directory under ``root``.

    When ``write_yaml`` is False the gate yaml is omitted; when
    ``final_status`` is None the file exists but lacks a ``decision`` block.
    """
    case = root / name
    case.mkdir(parents=True)
    if write_yaml:
        if final_status is None:
            content = "meta:\n  hotspot_id: HSP-X\n"
        else:
            content = (
                "meta:\n"
                f"  hotspot_id: {name.split('-2026')[0]}\n"
                "decision:\n"
                f"  final_status: {final_status}\n"
            )
        (case / "02-pipeline-gate.yaml").write_text(content, encoding="utf-8")
    return case


# --------------------------------------------------------------------------- #
# find_pool_cases
# --------------------------------------------------------------------------- #
def test_find_pool_cases_skips_dirs_without_gate_file(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    _write_case(cases_dir, "HSP-001-2026-05-01-alpha", final_status="watch")
    _write_case(cases_dir, "HSP-002-2026-05-01-beta", final_status="watch")
    # third dir exists but no gate yaml
    _write_case(cases_dir, "HSP-003-2026-05-01-gamma", write_yaml=False)

    found = find_pool_cases(cases_dir)
    names = {p.name for p in found}
    assert names == {
        "HSP-001-2026-05-01-alpha",
        "HSP-002-2026-05-01-beta",
    }


def test_find_pool_cases_status_filter(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    _write_case(cases_dir, "HSP-001-2026-05-01-alpha", final_status="watch")
    _write_case(cases_dir, "HSP-002-2026-05-01-beta", final_status="drop")
    _write_case(cases_dir, "HSP-003-2026-05-01-gamma", final_status="probe")

    only_watch = find_pool_cases(cases_dir, status_filter=["watch"])
    assert [p.name for p in only_watch] == ["HSP-001-2026-05-01-alpha"]

    watch_or_probe = find_pool_cases(cases_dir, status_filter=["watch", "probe"])
    assert [p.name for p in watch_or_probe] == [
        "HSP-001-2026-05-01-alpha",
        "HSP-003-2026-05-01-gamma",
    ]

    # None disables filtering.
    all_cases = find_pool_cases(cases_dir, status_filter=None)
    assert len(all_cases) == 3


def test_find_pool_cases_sorts_by_hotspot_id(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    # Create out of order to make sure sort is doing the work.
    _write_case(cases_dir, "HSP-010-2026-05-01-omega", final_status="watch")
    _write_case(cases_dir, "HSP-002-2026-05-01-beta", final_status="watch")
    _write_case(cases_dir, "HSP-001-2026-05-01-alpha", final_status="watch")

    found = find_pool_cases(cases_dir)
    assert [p.name for p in found] == [
        "HSP-001-2026-05-01-alpha",
        "HSP-002-2026-05-01-beta",
        "HSP-010-2026-05-01-omega",
    ]


def test_find_pool_cases_name_glob(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    _write_case(cases_dir, "HSP-001-2026-05-01-alpha", final_status="watch")
    _write_case(cases_dir, "HSP-002-2026-05-01-beta", final_status="watch")
    _write_case(cases_dir, "HSP-003-2026-05-02-alpha", final_status="watch")

    only_alpha = find_pool_cases(cases_dir, name_glob="*alpha")
    assert {p.name for p in only_alpha} == {
        "HSP-001-2026-05-01-alpha",
        "HSP-003-2026-05-02-alpha",
    }

    only_may1 = find_pool_cases(cases_dir, name_glob="*2026-05-01*")
    assert len(only_may1) == 2


def test_find_pool_cases_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert find_pool_cases(tmp_path / "does-not-exist") == []


# --------------------------------------------------------------------------- #
# run_pool_parallel
# --------------------------------------------------------------------------- #
def test_run_pool_parallel_all_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [tmp_path / f"case-{i}" for i in range(4)]
    for c in cases:
        c.mkdir()

    def fake_subprocess(
        case_dir: Path,
        mode: str,
        extra_args: list[str],
        **_: Any,
    ) -> dict:
        return {
            "case_dir": str(case_dir),
            "mode": mode,
            "returncode": 0,
            "stdout_tail": "ok",
            "stderr_tail": "",
            "duration_seconds": 0.01,
            "status": "passed",
        }

    monkeypatch.setattr(pool_runner, "run_case_subprocess", fake_subprocess)

    report = run_pool_parallel(
        cases,
        mode="inspect",
        max_workers=4,
        run_pipeline_script=tmp_path / "run_pipeline.py",
        timeout_per_case=5,
    )

    assert report["total"] == 4
    assert report["passed"] == 4
    assert report["failed"] == 0
    assert report["timeout"] == 0
    assert len(report["results"]) == 4
    for r in report["results"]:
        assert r["status"] == "passed"
        assert r["mode"] == "inspect"


def test_run_pool_parallel_mixed_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One timeout, one failure, two pass -> counts must reflect all three."""
    cases = [tmp_path / f"case-{i}" for i in range(4)]
    for c in cases:
        c.mkdir()

    def fake_subprocess(
        case_dir: Path,
        mode: str,
        extra_args: list[str],
        **_: Any,
    ) -> dict:
        name = case_dir.name
        if name == "case-0":
            status, rc = "timeout", -1
        elif name == "case-1":
            status, rc = "failed", 2
        else:
            status, rc = "passed", 0
        return {
            "case_dir": str(case_dir),
            "mode": mode,
            "returncode": rc,
            "stdout_tail": "",
            "stderr_tail": "" if status == "passed" else "boom",
            "duration_seconds": 0.05,
            "status": status,
        }

    monkeypatch.setattr(pool_runner, "run_case_subprocess", fake_subprocess)

    report = run_pool_parallel(
        cases,
        mode="discover",
        max_workers=2,
        run_pipeline_script=tmp_path / "run_pipeline.py",
        timeout_per_case=5,
    )

    assert report["total"] == 4
    assert report["passed"] == 2
    assert report["failed"] == 1
    assert report["timeout"] == 1


def test_run_pool_parallel_publish_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="publish"):
        run_pool_parallel(
            [tmp_path],
            mode="publish",
            run_pipeline_script=tmp_path / "run_pipeline.py",
        )


def test_run_pool_parallel_unknown_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="recognized"):
        run_pool_parallel(
            [tmp_path],
            mode="totally-bogus",
            run_pipeline_script=tmp_path / "run_pipeline.py",
        )


def test_run_pool_parallel_empty_cases(tmp_path: Path) -> None:
    report = run_pool_parallel(
        [],
        mode="inspect",
        run_pipeline_script=tmp_path / "run_pipeline.py",
    )
    assert report["total"] == 0
    assert report["results"] == []


def test_run_pool_parallel_invokes_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [tmp_path / f"case-{i}" for i in range(3)]
    for c in cases:
        c.mkdir()

    def fake_subprocess(case_dir: Path, mode: str, extra_args: list[str], **_: Any) -> dict:
        return {
            "case_dir": str(case_dir),
            "mode": mode,
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_seconds": 0.01,
            "status": "passed",
        }

    monkeypatch.setattr(pool_runner, "run_case_subprocess", fake_subprocess)

    seen: list[dict] = []
    run_pool_parallel(
        cases,
        mode="inspect",
        max_workers=2,
        run_pipeline_script=tmp_path / "run_pipeline.py",
        on_complete_callable=seen.append,
    )
    assert len(seen) == 3


# --------------------------------------------------------------------------- #
# format_pool_summary
# --------------------------------------------------------------------------- #
def test_format_pool_summary_includes_counts_and_slowest() -> None:
    report = {
        "total": 5,
        "passed": 3,
        "failed": 1,
        "timeout": 1,
        "duration_seconds": 12.3,
        "results": [
            {"case_dir": "/cases/a", "status": "passed", "duration_seconds": 0.5, "returncode": 0},
            {"case_dir": "/cases/b", "status": "passed", "duration_seconds": 9.9, "returncode": 0},
            {"case_dir": "/cases/c", "status": "failed", "duration_seconds": 1.5, "returncode": 1},
            {"case_dir": "/cases/d", "status": "timeout", "duration_seconds": 30.0, "returncode": -1},
            {"case_dir": "/cases/e", "status": "passed", "duration_seconds": 4.0, "returncode": 0},
        ],
    }
    text = format_pool_summary(report)

    assert "passed=3" in text
    assert "failed=1" in text
    assert "timeout=1" in text
    # slowest 3 by duration: d (30), b (9.9), e (4.0)
    assert "/cases/d" in text
    assert "/cases/b" in text
    assert "/cases/e" in text
    # failure list mentions both failed and timed-out cases
    assert "failures:" in text
    assert "/cases/c" in text


def test_format_pool_summary_empty_report() -> None:
    text = format_pool_summary({
        "total": 0, "passed": 0, "failed": 0, "timeout": 0,
        "duration_seconds": 0.0, "results": [],
    })
    assert "total=0" in text
    assert "passed=0" in text
    # no slowest / failures sections when there are no results
    assert "slowest:" not in text
    assert "failures:" not in text


# --------------------------------------------------------------------------- #
# argparse plumbing
# --------------------------------------------------------------------------- #
def test_register_pool_args_defaults() -> None:
    parser = argparse.ArgumentParser()
    register_pool_args(parser)
    args = parser.parse_args([])

    assert args.pool_cases_dir == "cases"
    assert args.pool_mode == "inspect"
    assert args.pool_status_filter == ""
    assert args.pool_name_glob == ""
    assert args.pool_max_workers == 4
    assert args.pool_timeout_per_case == 300
    assert args.pool_extra_args == ""
    assert args.pool_execute is False


def test_register_pool_args_rejects_publish() -> None:
    parser = argparse.ArgumentParser()
    register_pool_args(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--pool-mode", "publish"])


def test_pool_args_to_kwargs_parses_status_and_extras() -> None:
    parser = argparse.ArgumentParser()
    register_pool_args(parser)
    args = parser.parse_args([
        "--pool-cases-dir", "/tmp/foo",
        "--pool-mode", "discover",
        "--pool-status-filter", "draft, watch ,",
        "--pool-name-glob", "HSP-00*",
        "--pool-max-workers", "8",
        "--pool-timeout-per-case", "120",
        "--pool-extra-args", "--discover-limit 3 --discover-sources github",
        "--pool-execute",
    ])
    kw = pool_args_to_kwargs(args)

    assert kw["pool_cases_dir"] == "/tmp/foo"
    assert kw["pool_mode"] == "discover"
    assert kw["status_filter"] == ["draft", "watch"]
    assert kw["name_glob"] == "HSP-00*"
    assert kw["max_workers"] == 8
    assert kw["timeout_per_case"] == 120
    # shlex.split + auto-appended --execute
    assert kw["extra_args"] == [
        "--discover-limit", "3", "--discover-sources", "github", "--execute",
    ]


def test_pool_args_to_kwargs_empty_status_filter_is_none() -> None:
    parser = argparse.ArgumentParser()
    register_pool_args(parser)
    args = parser.parse_args([])
    kw = pool_args_to_kwargs(args)
    assert kw["status_filter"] is None
    assert kw["extra_args"] == []
