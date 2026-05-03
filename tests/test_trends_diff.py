"""Tests for ``agentflow_pipeline.trends_diff``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentflow_pipeline import trends_diff
from agentflow_pipeline.trends_diff import (
    detect_new_entries,
    detect_rising_entries,
    load_scan_history,
    main,
    promote_to_case,
    run_trends_diff,
    summarize_diff,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_scan(trends_dir: Path, folder: str, top: list[dict]) -> Path:
    folder_path = trends_dir / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    scan_path = folder_path / "scan.json"
    scan_path.write_text(
        json.dumps({"scanned_at": folder, "top": top}),
        encoding="utf-8",
    )
    return scan_path


def _entry(name: str, engagement: float, *, owner: str = "alice") -> dict:
    return {
        "name": f"{owner}/{name}",
        "url": f"https://github.com/{owner}/{name}",
        "engagement": engagement,
    }


# ---------------------------------------------------------------------------
# load_scan_history
# ---------------------------------------------------------------------------

def test_load_scan_history_returns_newest_first(tmp_path: Path) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-04-30-09", [_entry("a", 100)])
    _write_scan(trends, "2026-05-01-09", [_entry("b", 200)])
    _write_scan(trends, "2026-04-30-21", [_entry("c", 50)])

    history = load_scan_history(trends)
    assert [r["scanned_at"] for r in history] == [
        "2026-05-01-09",
        "2026-04-30-21",
        "2026-04-30-09",
    ]
    # path field should point at scan.json files
    for record in history:
        assert record["path"].name == "scan.json"
        assert "top" in record["scan_data"]


def test_load_scan_history_limit_truncates(tmp_path: Path) -> None:
    trends = tmp_path / "trends"
    for hour in (9, 12, 15, 18, 21):
        _write_scan(trends, f"2026-05-01-{hour:02d}", [_entry("x", hour)])
    history = load_scan_history(trends, limit=2)
    assert len(history) == 2
    assert history[0]["scanned_at"] == "2026-05-01-21"
    assert history[1]["scanned_at"] == "2026-05-01-18"


def test_load_scan_history_skips_malformed_json(tmp_path: Path) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-05-01-09", [_entry("good", 10)])
    bad = trends / "2026-05-01-12"
    bad.mkdir(parents=True)
    (bad / "scan.json").write_text("{not json", encoding="utf-8")

    history = load_scan_history(trends)
    assert len(history) == 1
    assert history[0]["scanned_at"] == "2026-05-01-09"


def test_load_scan_history_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert load_scan_history(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# detect_new_entries
# ---------------------------------------------------------------------------

def test_detect_new_entries_returns_only_first_seen(tmp_path: Path) -> None:
    baseline = [
        {"scan_data": {"top": [_entry("known", 500)]}, "scanned_at": "old"},
    ]
    latest = {
        "scanned_at": "2026-05-01-09",
        "top": [_entry("known", 600), _entry("fresh", 250, owner="bob")],
    }
    new_entries = detect_new_entries(latest, baseline, min_engagement=50)
    assert len(new_entries) == 1
    assert new_entries[0]["name"] == "bob/fresh"
    assert new_entries[0]["first_seen_at"] == "2026-05-01-09"


def test_detect_new_entries_filters_low_engagement() -> None:
    baseline: list[dict] = []  # nothing in baseline -> everything is new candidate
    latest = {
        "scanned_at": "now",
        "top": [_entry("loud", 200), _entry("quiet", 5)],
    }
    out = detect_new_entries(latest, baseline, min_engagement=50)
    names = {e["name"] for e in out}
    assert names == {"alice/loud"}


def test_detect_new_entries_canonicalises_url_across_scans() -> None:
    # baseline has the URL with .git suffix; latest has plain URL.  After
    # canonicalisation they should match and the entry should NOT be flagged
    # as new.
    baseline = [
        {
            "scan_data": {
                "top": [{"name": "alice/repo",
                          "url": "https://github.com/alice/repo.git",
                          "engagement": 200}],
            },
            "scanned_at": "old",
        }
    ]
    latest = {
        "scanned_at": "now",
        "top": [_entry("repo", 300)],
    }
    assert detect_new_entries(latest, baseline, min_engagement=50) == []


# ---------------------------------------------------------------------------
# detect_rising_entries
# ---------------------------------------------------------------------------

def test_detect_rising_entries_doubling_engagement_qualifies() -> None:
    # newest first
    history = [
        {"scanned_at": "t4", "scan_data": {"top": [_entry("rocket", 400)]}},
        {"scanned_at": "t3", "scan_data": {"top": [_entry("rocket", 300)]}},
        {"scanned_at": "t2", "scan_data": {"top": [_entry("rocket", 250)]}},
        {"scanned_at": "t1", "scan_data": {"top": [_entry("rocket", 100)]}},
    ]
    rising = detect_rising_entries(
        history, min_appearances=3, min_engagement_growth=0.5,
    )
    assert len(rising) == 1
    item = rising[0]
    assert item["entry"]["name"] == "alice/rocket"
    assert item["growth_ratio"] == pytest.approx(3.0)  # (400 - 100) / 100
    assert item["current"] == 400.0
    # engagement_history must be chronological (oldest -> newest)
    timestamps = [pt["scanned_at"] for pt in item["engagement_history"]]
    assert timestamps == ["t1", "t2", "t3", "t4"]


def test_detect_rising_entries_requires_min_appearances() -> None:
    history = [
        {"scanned_at": "t2", "scan_data": {"top": [_entry("once", 500)]}},
        {"scanned_at": "t1", "scan_data": {"top": [_entry("other", 10)]}},
    ]
    rising = detect_rising_entries(
        history, min_appearances=3, min_engagement_growth=0.5,
    )
    assert rising == []


def test_detect_rising_entries_filters_flat_growth() -> None:
    history = [
        {"scanned_at": "t3", "scan_data": {"top": [_entry("flat", 105)]}},
        {"scanned_at": "t2", "scan_data": {"top": [_entry("flat", 100)]}},
        {"scanned_at": "t1", "scan_data": {"top": [_entry("flat", 100)]}},
    ]
    # 5% growth, well below default 50% threshold
    assert detect_rising_entries(history) == []


# ---------------------------------------------------------------------------
# summarize_diff
# ---------------------------------------------------------------------------

def test_summarize_diff_includes_required_sections() -> None:
    new_entries = [{**_entry("hot", 300), "first_seen_at": "2026-05-01-09"}]
    rising_entries = [{
        "entry": _entry("rising", 500),
        "engagement_history": [
            {"scanned_at": "t1", "engagement": 100},
            {"scanned_at": "t2", "engagement": 200},
            {"scanned_at": "t3", "engagement": 500},
        ],
        "growth_ratio": 4.0,
        "first_seen": "t1",
        "current": 500.0,
    }]
    md = summarize_diff(new_entries, rising_entries)
    assert "## New entries" in md
    assert "## Rising entries" in md
    assert "alice/hot" in md
    assert "alice/rising" in md


def test_summarize_diff_handles_empty_lists() -> None:
    md = summarize_diff([], [])
    assert "## New entries" in md
    assert "## Rising entries" in md
    assert "No new entries" in md
    assert "No rising entries" in md


# ---------------------------------------------------------------------------
# promote_to_case
# ---------------------------------------------------------------------------

def test_promote_to_case_dry_run_does_not_invoke_scaffold(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_scaffold(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    plan = promote_to_case(
        _entry("hot", 500),
        root=tmp_path,
        dry_run=True,
        scaffold_callable=fake_scaffold,
    )
    assert calls == []
    assert plan["dry_run"] is True
    assert plan["returncode"] is None
    assert "--hotspot-name" in plan["argv"]


def test_promote_to_case_real_invokes_scaffold_with_hotspot_name(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_scaffold(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    entry = _entry("hot-thing", 999)
    plan = promote_to_case(
        entry,
        root=tmp_path,
        owner="founder",
        project_shape="data_pipeline",
        dry_run=False,
        scaffold_callable=fake_scaffold,
    )
    assert len(calls) == 1
    argv = calls[0]
    assert "--hotspot-name" in argv
    name_index = argv.index("--hotspot-name")
    assert argv[name_index + 1] == "alice/hot-thing"
    assert "--owner" in argv and argv[argv.index("--owner") + 1] == "founder"
    assert "--project-shape" in argv
    assert plan["returncode"] == 0
    assert plan["dry_run"] is False


# ---------------------------------------------------------------------------
# run_trends_diff (integration)
# ---------------------------------------------------------------------------

def test_run_trends_diff_writes_summary_and_counts(tmp_path: Path) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-04-30-09", [_entry("ongoing", 100)])
    _write_scan(trends, "2026-04-30-21", [_entry("ongoing", 200)])
    _write_scan(trends, "2026-05-01-09", [
        _entry("ongoing", 350),
        _entry("brand-new", 150, owner="bob"),
    ])

    result = run_trends_diff(trends)
    assert result["latest_scanned_at"] == "2026-05-01-09"
    assert result["new_count"] == 1
    assert result["rising_count"] >= 1
    assert result["promoted_count"] == 0
    summary_path = Path(result["summary_path"])
    assert summary_path.exists()
    assert summary_path.name == "diff-2026-05-01-09.md"
    body = summary_path.read_text(encoding="utf-8")
    assert "bob/brand-new" in body


def test_run_trends_diff_auto_promote_dry_run_collects_high_engagement(tmp_path: Path) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-04-30-09", [_entry("known", 100)])
    _write_scan(trends, "2026-05-01-09", [
        _entry("known", 200),
        _entry("huge", 400, owner="bob"),
        _entry("small", 60, owner="bob"),
    ])

    result = run_trends_diff(
        trends,
        auto_promote=True,
        promote_min_engagement=200,
        root=tmp_path,
    )
    assert result["promoted_count"] == 1
    assert result["promoted"][0]["dry_run"] is True
    assert result["promoted"][0]["hotspot_name"] == "bob/huge"


def test_run_trends_diff_empty_trends_returns_zero_counts(tmp_path: Path) -> None:
    result = run_trends_diff(tmp_path / "trends")
    assert result == {
        "new_count": 0,
        "rising_count": 0,
        "promoted_count": 0,
        "summary_path": None,
        "promoted": [],
        "latest_scanned_at": "",
    }


# ---------------------------------------------------------------------------
# main() / CLI
# ---------------------------------------------------------------------------

def test_main_diff_subcommand_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-05-01-09", [_entry("solo", 200)])
    rc = main([
        "diff",
        "--root", str(tmp_path),
        "--trends-dir", str(trends),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "new_count" in payload
    assert payload["latest_scanned_at"] == "2026-05-01-09"
    assert Path(payload["summary_path"]).exists()


def test_main_promote_dry_run_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-04-30-09", [_entry("known", 100)])
    _write_scan(trends, "2026-05-01-09", [
        _entry("known", 150),
        _entry("rocket", 500, owner="bob"),
    ])

    calls: list[list[str]] = []

    def fake_scaffold(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(trends_diff, "_default_scaffold_callable", fake_scaffold)

    rc = main([
        "promote",
        "--root", str(tmp_path),
        "--trends-dir", str(trends),
        "--min-engagement", "200",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert payload["promoted_count"] == 1
    assert payload["promoted"][0]["dry_run"] is True
    # In dry-run mode the scaffold callable must NOT be invoked.
    assert calls == []


def test_main_promote_apply_invokes_scaffold(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-04-30-09", [_entry("known", 100)])
    _write_scan(trends, "2026-05-01-09", [
        _entry("known", 150),
        _entry("rocket", 500, owner="bob"),
    ])

    calls: list[list[str]] = []

    def fake_scaffold(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(trends_diff, "_default_scaffold_callable", fake_scaffold)

    rc = main([
        "promote",
        "--root", str(tmp_path),
        "--trends-dir", str(trends),
        "--min-engagement", "200",
        "--apply",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["promoted_count"] == 1
    assert calls, "scaffold callable should be invoked once with --apply"
    argv = calls[0]
    assert "--hotspot-name" in argv
    assert argv[argv.index("--hotspot-name") + 1] == "bob/rocket"


def test_main_promote_apply_with_dry_run_flag_stays_dry(tmp_path: Path,
                                                          capsys: pytest.CaptureFixture[str],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    trends = tmp_path / "trends"
    _write_scan(trends, "2026-05-01-09", [_entry("rocket", 500, owner="bob")])

    calls: list[list[str]] = []

    def fake_scaffold(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(trends_diff, "_default_scaffold_callable", fake_scaffold)

    rc = main([
        "promote",
        "--root", str(tmp_path),
        "--trends-dir", str(trends),
        "--min-engagement", "200",
        "--apply",
        "--dry-run",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert calls == []
