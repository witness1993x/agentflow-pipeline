"""Tests for ``agentflow_pipeline.scan_hotspots`` (the agentflow-scan CLI)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentflow_pipeline import scan_hotspots, trends_diff
from agentflow_pipeline.scan_hotspots import (
    ALL_SOURCES,
    DEFAULT_QUERIES,
    DEFAULT_SOURCES,
    _guess_lang_from_candidate,
    _maybe_auto_promote,
    _maybe_notify_lark,
    aggregate_scan_results,
    discover_shipped_repos,
    main,
    register_scan_args,
    run_scan,
    scan_github_trending,
    scan_hackernews,
    scan_reddit,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_payload(name: str, owner: str, stars: int, url: str | None = None) -> dict:
    return {
        "name": name,
        "owner": {"login": owner},
        "url": url or f"https://github.com/{owner}/{name}",
        "description": f"{owner}/{name} desc",
        "stargazersCount": stars,
        "language": "Python",
        "createdAt": "2026-04-15T00:00:00Z",
        "pushedAt": "2026-04-30T00:00:00Z",
    }


# --------------------------------------------------------------------------- #
# scan_github_trending
# --------------------------------------------------------------------------- #
def test_scan_github_trending_calls_gh_search_with_correct_args_and_normalizes() -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        captured.append(list(cmd))
        return _FakeCompleted(0, json.dumps([_gh_payload("repo1", "alice", 200)]))

    hits = scan_github_trending(
        ["solana ai agent"],
        stars_min=42,
        days=14,
        limit_per_query=7,
        run_command=fake_run,
    )

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[:3] == ["gh", "search", "repos"]
    full_query = cmd[3]
    assert full_query.startswith("solana ai agent created:>")
    assert "stars:>42" in full_query
    assert "--sort" in cmd and cmd[cmd.index("--sort") + 1] == "stars"
    assert "--order" in cmd and cmd[cmd.index("--order") + 1] == "desc"
    assert "--limit" in cmd and cmd[cmd.index("--limit") + 1] == "7"
    assert "--json" in cmd

    assert len(hits) == 1
    h = hits[0]
    assert h["source"] == "github"
    assert h["query"] == "solana ai agent"
    assert h["name"] == "repo1"
    assert h["owner"] == "alice"
    assert h["url"] == "https://github.com/alice/repo1"
    assert h["stars"] == 200
    assert h["language"] == "Python"
    assert h["description"] == "alice/repo1 desc"
    assert h["created_at"] == "2026-04-15T00:00:00Z"
    assert h["pushed_at"] == "2026-04-30T00:00:00Z"


def test_scan_github_trending_failed_query_does_not_raise() -> None:
    def fake_run(cmd, cwd=None):
        # First query succeeds, second fails non-zero.
        if "good" in cmd[3]:
            return _FakeCompleted(0, json.dumps([_gh_payload("ok", "bob", 50)]))
        return _FakeCompleted(1, "", "rate limited")

    hits = scan_github_trending(
        ["good query", "bad query"],
        run_command=fake_run,
    )
    # Failure for the bad query is swallowed; the good query still surfaces.
    assert len(hits) == 1
    assert hits[0]["name"] == "ok"


def test_scan_github_trending_handles_missing_gh_binary() -> None:
    def fake_run(cmd, cwd=None):
        raise FileNotFoundError("gh: command not found")

    hits = scan_github_trending(["q"], run_command=fake_run)
    assert hits == []


# --------------------------------------------------------------------------- #
# scan_hackernews
# --------------------------------------------------------------------------- #
def test_scan_hackernews_returns_normalized_hits(monkeypatch) -> None:
    captured_urls: list[str] = []

    def fake_http(url: str, headers=None, timeout: int = 30) -> Any:
        captured_urls.append(url)
        return {
            "hits": [
                {
                    "objectID": "555",
                    "title": "Show HN: cool repo",
                    "url": "https://github.com/foo/bar",
                    "points": 42,
                    "num_comments": 8,
                    "created_at": "2026-04-29T00:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(scan_hotspots, "_http_get_json", fake_http)

    hits = scan_hackernews(["solana"], days=7, hits_per_query=5)
    assert len(captured_urls) == 1
    assert "hn.algolia.com/api/v1/search" in captured_urls[0]
    assert "numericFilters=created_at_i" in captured_urls[0]
    assert "tags=story" in captured_urls[0]

    assert len(hits) == 1
    h = hits[0]
    assert h["source"] == "hackernews"
    assert h["query"] == "solana"
    assert h["title"] == "Show HN: cool repo"
    assert h["url"] == "https://github.com/foo/bar"
    assert h["points"] == 42
    assert h["num_comments"] == 8
    assert h["created_at"] == "2026-04-29T00:00:00Z"


def test_scan_hackernews_per_query_failure_is_caught(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_http(url, headers=None, timeout: int = 30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated HTTP error")
        return {
            "hits": [
                {
                    "objectID": "1",
                    "title": "ok",
                    "url": "",
                    "points": 1,
                    "num_comments": 0,
                    "created_at": "2026-04-29T00:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(scan_hotspots, "_http_get_json", fake_http)

    hits = scan_hackernews(["bad", "good"], days=7, hits_per_query=5)
    assert len(hits) == 1
    assert hits[0]["title"] == "ok"
    # No url -> should fall back to news.ycombinator.com permalink
    assert hits[0]["url"] == "https://news.ycombinator.com/item?id=1"


# --------------------------------------------------------------------------- #
# scan_reddit
# --------------------------------------------------------------------------- #
def test_scan_reddit_rate_limited_returns_empty_list(monkeypatch) -> None:
    """Even if every sub-call is blocked, scan_reddit should not raise."""

    def always_fail(url, headers=None, timeout: int = 30):
        raise RuntimeError("HTTP 429: rate limited")

    monkeypatch.setattr(scan_hotspots, "_http_get_json", always_fail)

    hits = scan_reddit(["solana"], subreddits=["ethereum", "solana"], hits_per_query=5)
    assert hits == []


def test_scan_reddit_extracts_github_url_from_selftext(monkeypatch) -> None:
    def fake_http(url, headers=None, timeout: int = 30):
        return {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "p1",
                            "subreddit": "ethereum",
                            "title": "look at this",
                            "selftext": "found https://github.com/foo/bar last week",
                            "permalink": "/r/ethereum/comments/p1/look/",
                            "url": "https://reddit.com/r/ethereum/comments/p1/look/",
                            "score": 100,
                            "num_comments": 25,
                            "created_utc": 1714521600,
                        }
                    }
                ]
            }
        }

    monkeypatch.setattr(scan_hotspots, "_http_get_json", fake_http)

    hits = scan_reddit(["q"], subreddits=["ethereum"], hits_per_query=5)
    assert len(hits) == 1
    h = hits[0]
    assert h["source"] == "reddit"
    assert h["url"] == "https://github.com/foo/bar"
    assert h["score"] == 100
    assert h["num_comments"] == 25
    assert h["subreddit"] == "ethereum"


# --------------------------------------------------------------------------- #
# aggregate_scan_results
# --------------------------------------------------------------------------- #
def test_aggregate_merges_same_github_url_across_sources() -> None:
    gh = [
        {
            "source": "github",
            "query": "q",
            "name": "bar",
            "owner": "foo",
            "url": "https://github.com/foo/bar",
            "stars": 200,
            "language": "Python",
            "description": "the desc",
            "created_at": "",
            "pushed_at": "",
        }
    ]
    hn = [
        {
            "source": "hackernews",
            "query": "q",
            "title": "Show HN: foo/bar",
            "url": "https://github.com/foo/bar",
            "points": 50,
            "num_comments": 10,
            "created_at": "",
        }
    ]
    rd = [
        {
            "source": "reddit",
            "query": "q",
            "subreddit": "ethereum",
            "title": "look",
            "url": "https://github.com/foo/bar/",  # trailing slash should still merge
            "score": 30,
            "num_comments": 5,
            "created_at": "",
        }
    ]
    agg = aggregate_scan_results(gh, hn, rd, top_n=10)

    assert agg["unique_count"] == 1
    assert agg["duplicates_merged"] == 2
    top = agg["top"]
    assert len(top) == 1
    base = top[0]
    assert base["source"] == "github"
    # GitHub stars 200 + HN points+comments 60 + Reddit score+comments 35 = 295
    assert base["engagement"] == 200 + 60 + 35
    assert isinstance(base["sources_seen"], list)
    assert sorted(base["sources_seen"]) == ["github", "hackernews", "reddit"]
    # by_source counts reflect raw input volumes regardless of dedup.
    assert agg["by_source"] == {"github": 1, "hackernews": 1, "reddit": 1}


def test_aggregate_engagement_sort_orders_top_by_combined_score() -> None:
    gh = [
        {
            "source": "github",
            "query": "q",
            "name": "lo",
            "owner": "x",
            "url": "https://github.com/x/lo",
            "stars": 10,
            "language": "",
            "description": "",
            "created_at": "",
            "pushed_at": "",
        },
        {
            "source": "github",
            "query": "q",
            "name": "hi",
            "owner": "x",
            "url": "https://github.com/x/hi",
            "stars": 999,
            "language": "",
            "description": "",
            "created_at": "",
            "pushed_at": "",
        },
    ]
    hn = [
        {
            "source": "hackernews",
            "query": "q",
            "title": "mid",
            "url": "https://news.ycombinator.com/item?id=1",
            "points": 100,
            "num_comments": 50,
            "created_at": "",
        }
    ]
    rd = [
        {
            "source": "reddit",
            "query": "q",
            "subreddit": "x",
            "title": "rd",
            "url": "https://reddit.com/r/x/comments/abc/",
            "score": 20,
            "num_comments": 30,
            "created_at": "",
        }
    ]
    agg = aggregate_scan_results(gh, hn, rd, top_n=10)
    engagements = [r["engagement"] for r in agg["top"]]
    assert engagements == sorted(engagements, reverse=True)
    assert agg["top"][0]["engagement"] == 999  # hi GitHub repo wins
    assert agg["top"][1]["engagement"] == 150  # HN mid
    assert agg["top"][2]["engagement"] == 50  # Reddit
    assert agg["top"][3]["engagement"] == 10  # lo GitHub
    # top_n truncation
    agg2 = aggregate_scan_results(gh, hn, rd, top_n=2)
    assert len(agg2["top"]) == 2
    assert len(agg2["all"]) == 4


# --------------------------------------------------------------------------- #
# run_scan persistence
# --------------------------------------------------------------------------- #
def _stub_run_scan_io(monkeypatch) -> None:
    """Patch HN + Reddit HTTP calls to return canned data."""

    def fake_http(url, headers=None, timeout: int = 30):
        if "hn.algolia.com" in url:
            return {
                "hits": [
                    {
                        "objectID": "9",
                        "title": "Show HN: alice/demo",
                        "url": "https://github.com/alice/demo",
                        "points": 30,
                        "num_comments": 5,
                        "created_at": "2026-04-29T00:00:00Z",
                    }
                ]
            }
        return {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "rd1",
                            "subreddit": "ethereum",
                            "title": "look at alice/demo",
                            "selftext": "https://github.com/alice/demo rocks",
                            "permalink": "/r/ethereum/comments/rd1/x/",
                            "url": "https://reddit.com/r/ethereum/comments/rd1/x/",
                            "score": 20,
                            "num_comments": 4,
                            "created_utc": 1714521600,
                        }
                    }
                ]
            }
        }

    monkeypatch.setattr(scan_hotspots, "_http_get_json", fake_http)


def _fake_gh_run(cmd, cwd=None):
    return _FakeCompleted(0, json.dumps([_gh_payload("demo", "alice", 100)]))


def test_run_scan_writes_md_and_json_to_hourly_subdir(monkeypatch, tmp_path) -> None:
    _stub_run_scan_io(monkeypatch)

    agg = run_scan(
        ["solana ai agent"],
        output_dir=tmp_path / "trends",
        sources=list(DEFAULT_SOURCES),
        github_stars_min=10,
        github_days=30,
        hn_days=7,
        reddit_subreddits=["ethereum"],
        top_n=10,
        run_command=_fake_gh_run,
    )

    md_path = Path(agg["output_md_path"])
    json_path = Path(agg["output_json_path"])
    assert md_path.exists()
    assert json_path.exists()
    # Hourly subdirectory naming: YYYY-MM-DD-HH (2 digits each).
    hour_dir_name = md_path.parent.name
    assert len(hour_dir_name) == len("YYYY-MM-DD-HH")
    parts = hour_dir_name.split("-")
    assert len(parts) == 4
    assert len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2 and len(parts[3]) == 2
    assert json_path.parent == md_path.parent
    assert json_path.parent.parent == tmp_path / "trends"

    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    # All three sources should merge into a single canonical github/alice/demo.
    assert parsed["unique_count"] == 1
    assert parsed["duplicates_merged"] == 2

    md_text = md_path.read_text(encoding="utf-8")
    assert "agentflow-scan snapshot" in md_text
    assert "alice/demo" in md_text
    assert "https://github.com/alice/demo" in md_text


def test_run_scan_format_json_only(monkeypatch, tmp_path) -> None:
    _stub_run_scan_io(monkeypatch)

    agg = run_scan(
        ["solana"],
        output_dir=tmp_path / "trends",
        sources=["github"],
        formats=("json",),
        run_command=_fake_gh_run,
    )
    assert agg["output_md_path"] is None
    assert agg["output_json_path"] is not None
    json_path = Path(agg["output_json_path"])
    assert json_path.exists()
    md_sibling = json_path.parent / "scan.md"
    assert not md_sibling.exists()


def test_run_scan_same_hour_overwrites_different_hour_does_not(monkeypatch, tmp_path) -> None:
    """Same hour invocations are intentionally idempotent (overwrite).

    Different hours never collide. We simulate by injecting different ``now``
    values rather than waiting on the wall clock.
    """
    _stub_run_scan_io(monkeypatch)
    from datetime import datetime, timezone

    t_a = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_a_later = datetime(2026, 5, 1, 10, 30, 0, tzinfo=timezone.utc)
    t_b = datetime(2026, 5, 1, 11, 0, 0, tzinfo=timezone.utc)

    out_root = tmp_path / "trends"
    agg1 = run_scan(
        ["q"],
        output_dir=out_root,
        sources=["github"],
        run_command=_fake_gh_run,
        now=t_a,
    )
    agg2 = run_scan(
        ["q"],
        output_dir=out_root,
        sources=["github"],
        run_command=_fake_gh_run,
        now=t_a_later,  # same hour -> overwrites
    )
    agg3 = run_scan(
        ["q"],
        output_dir=out_root,
        sources=["github"],
        run_command=_fake_gh_run,
        now=t_b,  # different hour -> separate dir
    )

    assert Path(agg1["output_dir"]) == Path(agg2["output_dir"])
    assert Path(agg1["output_dir"]) != Path(agg3["output_dir"])
    # Both hour dirs should exist.
    assert (out_root / "2026-05-01-10").is_dir()
    assert (out_root / "2026-05-01-11").is_dir()


# --------------------------------------------------------------------------- #
# CLI / main()
# --------------------------------------------------------------------------- #
def test_register_scan_args_defaults() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    register_scan_args(parser)
    parsed = parser.parse_args([])
    assert parsed.queries == ",".join(DEFAULT_QUERIES)
    assert parsed.sources == ",".join(DEFAULT_SOURCES)
    assert parsed.top_n == 30
    assert parsed.format == "both"
    assert parsed.dry_run is False
    assert parsed.quiet is False


def test_main_dry_run_parses_and_resolves_paths(tmp_path, capsys, monkeypatch) -> None:
    out = tmp_path / "trends-test"
    rc = main(
        [
            "--queries",
            "solana,defi mcp",
            "--sources",
            "hackernews,reddit",
            "--root",
            str(tmp_path),
            "--output-dir",
            str(out),
            "--dry-run",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert plan["dry_run"] is True
    assert plan["queries"] == ["solana", "defi mcp"]
    assert plan["sources"] == ["hackernews", "reddit"]
    assert plan["output_dir"] == str(out.resolve())


def test_main_runs_with_stubbed_io_and_returns_zero(monkeypatch, tmp_path, capsys) -> None:
    _stub_run_scan_io(monkeypatch)

    monkeypatch.setattr(
        scan_hotspots, "default_run_command", lambda cmd, cwd=None: _fake_gh_run(cmd, cwd)
    )

    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github,hackernews,reddit",
            "--reddit-subreddits",
            "ethereum",
            "--output-dir",
            str(tmp_path / "trends"),
            "--top-n",
            "5",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "scanned 1 queries" in captured.out
    # Output files should exist
    files = list((tmp_path / "trends").rglob("scan.*"))
    suffixes = sorted(f.suffix for f in files)
    assert ".json" in suffixes
    assert ".md" in suffixes


def test_main_returns_one_when_all_sources_blocked(monkeypatch, tmp_path, capsys) -> None:
    """Every source blocked -> exit code 1."""

    def blocked_http(url, headers=None, timeout: int = 30):
        raise RuntimeError("HTTP 429")

    def blocked_gh(cmd, cwd=None):
        return _FakeCompleted(1, "", "auth failed")

    monkeypatch.setattr(scan_hotspots, "_http_get_json", blocked_http)
    monkeypatch.setattr(scan_hotspots, "default_run_command", blocked_gh)

    rc = main(
        [
            "--queries",
            "solana",
            "--sources",
            "github,hackernews,reddit",
            "--reddit-subreddits",
            "ethereum",
            "--output-dir",
            str(tmp_path / "trends"),
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "raw=0" in out
    # Still wrote out files (the snapshot is empty but valid).
    files = list((tmp_path / "trends").rglob("scan.json"))
    assert len(files) == 1
    parsed = json.loads(files[0].read_text(encoding="utf-8"))
    assert parsed["unique_count"] == 0


def test_main_queries_file_overrides_queries(monkeypatch, tmp_path, capsys) -> None:
    """``--queries-file`` should fully replace ``--queries``."""
    qfile = tmp_path / "queries.txt"
    qfile.write_text(
        "# comment line\nfile-query-one\n\n  file-query-two  \n",
        encoding="utf-8",
    )
    rc = main(
        [
            "--queries",
            "ignored,me-too",
            "--queries-file",
            str(qfile),
            "--sources",
            "github",
            "--output-dir",
            str(tmp_path / "trends"),
            "--dry-run",
        ]
    )
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["queries"] == ["file-query-one", "file-query-two"]


# --------------------------------------------------------------------------- #
# discover_shipped_repos / _guess_lang_from_candidate
# --------------------------------------------------------------------------- #
def _write_case(
    root: Path,
    *,
    case_dir: str,
    hotspot_id: str,
    final_status: str,
    owner: str,
    repo_name: str,
    project_shape: str | None = "data_pipeline",
    repo_meta_lang: str | None = None,
    candidate_lang: str | None = "TypeScript",
) -> Path:
    cfg: dict[str, Any] = {
        "meta": {"hotspot_id": hotspot_id},
        "repo_plan": {"github_owner": owner, "repo_name": repo_name},
        "decision": {"final_status": final_status},
        "gate_2_project_shape": {"project_shape": project_shape}
        if project_shape is not None
        else {},
    }
    if repo_meta_lang is not None:
        cfg["repo_meta"] = {"language": repo_meta_lang}
    if candidate_lang is not None:
        cfg["gate_3_repo_routing"] = {
            "candidate_repos": [
                {"language": candidate_lang, "name": "fake/example"},
            ]
        }
    case_path = root / "cases" / case_dir
    case_path.mkdir(parents=True, exist_ok=True)
    gate = case_path / "02-pipeline-gate.yaml"
    gate.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return gate


def test_discover_shipped_repos_filters_and_sorts(tmp_path: Path) -> None:
    # HSP-001: probe (not publish) -> excluded
    _write_case(
        tmp_path,
        case_dir="HSP-001-2026-05-01-solana",
        hotspot_id="HSP-001",
        final_status="probe",
        owner="witness1993x",
        repo_name="solana-thing",
    )
    # HSP-002: publish + complete -> included
    _write_case(
        tmp_path,
        case_dir="HSP-002-2026-05-01-wallet-pnl",
        hotspot_id="HSP-002",
        final_status="publish",
        owner="witness1993x",
        repo_name="wallet-pnl-tracker",
        project_shape="data_pipeline",
        candidate_lang="Python",
    )
    # HSP-003: publish but missing owner -> excluded
    _write_case(
        tmp_path,
        case_dir="HSP-003-2026-05-02-evm",
        hotspot_id="HSP-003",
        final_status="publish",
        owner="",
        repo_name="evm-whale-pulse",
    )
    # HSP-004: publish + complete -> included
    _write_case(
        tmp_path,
        case_dir="HSP-004-2026-05-02-stable-depeg",
        hotspot_id="HSP-004",
        final_status="publish",
        owner="witness1993x",
        repo_name="stable-depeg-radar",
        project_shape="dashboard",
        candidate_lang="Rust",
    )

    shipped = discover_shipped_repos(tmp_path)
    assert [r["hotspot_id"] for r in shipped] == ["HSP-002", "HSP-004"]

    r2, r4 = shipped
    assert r2 == {
        "hotspot_id": "HSP-002",
        "name": "wallet-pnl-tracker",
        "url": "https://github.com/witness1993x/wallet-pnl-tracker",
        "language": "Python",
        "shape": "data_pipeline",
    }
    assert r4["url"] == "https://github.com/witness1993x/stable-depeg-radar"
    assert r4["language"] == "Rust"
    assert r4["shape"] == "dashboard"


def test_discover_shipped_repos_skips_unparseable_yaml(tmp_path: Path) -> None:
    bad_dir = tmp_path / "cases" / "HSP-666-broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "02-pipeline-gate.yaml").write_text(
        "this: is: not: valid: yaml: [", encoding="utf-8"
    )
    # Plus one valid published case, to confirm we don't abort the whole walk.
    _write_case(
        tmp_path,
        case_dir="HSP-007-2026-05-03-good",
        hotspot_id="HSP-007",
        final_status="publish",
        owner="witness1993x",
        repo_name="good-repo",
        candidate_lang="Go",
    )
    shipped = discover_shipped_repos(tmp_path)
    assert [r["hotspot_id"] for r in shipped] == ["HSP-007"]
    assert shipped[0]["language"] == "Go"


def test_discover_shipped_repos_missing_root_returns_empty(tmp_path: Path) -> None:
    # tmp_path has no `cases/` subdir at all.
    assert discover_shipped_repos(tmp_path) == []
    # Likewise a totally non-existent dir.
    assert discover_shipped_repos(tmp_path / "nope" / "nada") == []


def test_guess_lang_from_candidate_three_modes() -> None:
    # Both repo_meta.language and candidate[0].language present -> caller
    # prefers repo_meta in discover_shipped_repos, but the helper itself
    # only inspects candidates. Verify all three modes here.
    cfg_with_candidate = {
        "gate_3_repo_routing": {
            "candidate_repos": [{"language": "TypeScript"}, {"language": "Rust"}]
        }
    }
    assert _guess_lang_from_candidate(cfg_with_candidate) == "TypeScript"

    cfg_no_lang = {
        "gate_3_repo_routing": {"candidate_repos": [{"name": "no-lang-here"}]}
    }
    assert _guess_lang_from_candidate(cfg_no_lang) is None

    cfg_empty = {"gate_3_repo_routing": {"candidate_repos": []}}
    assert _guess_lang_from_candidate(cfg_empty) is None
    assert _guess_lang_from_candidate({}) is None

    # And also: repo_meta.language is the discover-level preference. Build a
    # full case where repo_meta wins over the candidate.
    cfg_repo_meta_path: dict[str, Any] = {
        "meta": {"hotspot_id": "HSP-100"},
        "repo_plan": {"github_owner": "w", "repo_name": "r"},
        "decision": {"final_status": "publish"},
        "gate_2_project_shape": {"project_shape": "lib"},
        "repo_meta": {"language": "Elixir"},
        "gate_3_repo_routing": {"candidate_repos": [{"language": "Python"}]},
    }
    # Run via discover_shipped_repos to assert preference.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        case_dir = root / "cases" / "HSP-100-x"
        case_dir.mkdir(parents=True)
        (case_dir / "02-pipeline-gate.yaml").write_text(
            yaml.safe_dump(cfg_repo_meta_path), encoding="utf-8"
        )
        shipped = discover_shipped_repos(root)
        assert shipped == [
            {
                "hotspot_id": "HSP-100",
                "name": "r",
                "url": "https://github.com/w/r",
                "language": "Elixir",
                "shape": "lib",
            }
        ]


# --------------------------------------------------------------------------- #
# --notify-lark integration on the CLI
# --------------------------------------------------------------------------- #
class _NotifySpy:
    """Capture call args/kwargs to notify_scan_complete; produce a tunable result."""

    def __init__(
        self,
        *,
        result: dict | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.result = result or {
            "sent": True,
            "skipped_reason": None,
            "card_title": "agentflow scan",
            "body_size_bytes": 1234,
        }
        self.raise_exc = raise_exc

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def test_main_no_notify_lark_does_not_call_notifier(
    monkeypatch, tmp_path, capsys
) -> None:
    """Without --notify-lark, the notifier must never fire."""
    _stub_run_scan_io(monkeypatch)
    monkeypatch.setattr(
        scan_hotspots, "default_run_command", lambda cmd, cwd=None: _fake_gh_run(cmd, cwd)
    )
    spy = _NotifySpy()
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github,hackernews,reddit",
            "--reddit-subreddits",
            "ethereum",
            "--output-dir",
            str(tmp_path / "trends"),
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    assert spy.calls == []
    out = capsys.readouterr().out
    assert "[lark]" not in out


def test_main_notify_lark_dry_run_calls_notifier_with_dry_run_kwarg(
    monkeypatch, tmp_path, capsys
) -> None:
    _stub_run_scan_io(monkeypatch)
    monkeypatch.setattr(
        scan_hotspots, "default_run_command", lambda cmd, cwd=None: _fake_gh_run(cmd, cwd)
    )
    spy = _NotifySpy()
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github",
            "--output-dir",
            str(tmp_path / "trends"),
            "--root",
            str(tmp_path),
            "--notify-lark",
            "--lark-dry-run",
        ]
    )
    assert rc == 0
    assert len(spy.calls) == 1
    call_kwargs = spy.calls[0]["kwargs"]
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["framework_repo_url"] == (
        "https://github.com/witness1993x/agentflow-pipeline"
    )
    # No template provided -> trends_view_url is None.
    assert call_kwargs["trends_view_url"] is None
    # scan_result + shipped_repos passed positionally-by-keyword per contract.
    assert "scan_result" in call_kwargs
    assert isinstance(call_kwargs["scan_result"], dict)
    assert "unique_count" in call_kwargs["scan_result"]
    assert call_kwargs["shipped_repos"] == []  # tmp root has no cases/
    out = capsys.readouterr().out
    assert "[lark] sent=True reason=None" in out


def test_main_notify_lark_failure_never_breaks_scan(
    monkeypatch, tmp_path, capsys
) -> None:
    """If notify_scan_complete raises, scan must still exit 0."""
    _stub_run_scan_io(monkeypatch)
    monkeypatch.setattr(
        scan_hotspots, "default_run_command", lambda cmd, cwd=None: _fake_gh_run(cmd, cwd)
    )
    spy = _NotifySpy(raise_exc=RuntimeError("lark webhook 500"))
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github",
            "--output-dir",
            str(tmp_path / "trends"),
            "--root",
            str(tmp_path),
            "--notify-lark",
        ]
    )
    assert rc == 0
    assert len(spy.calls) == 1
    captured = capsys.readouterr()
    assert "notify failed (caught)" in captured.err
    assert "lark webhook 500" in captured.err


def test_main_notify_lark_trends_url_template_is_substituted(
    monkeypatch, tmp_path, capsys
) -> None:
    _stub_run_scan_io(monkeypatch)
    monkeypatch.setattr(
        scan_hotspots, "default_run_command", lambda cmd, cwd=None: _fake_gh_run(cmd, cwd)
    )
    spy = _NotifySpy()
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    template = "https://gh.com/r/blob/main/trends/{date}/scan.md"
    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github",
            "--output-dir",
            str(tmp_path / "trends"),
            "--root",
            str(tmp_path),
            "--notify-lark",
            "--lark-trends-view-url-template",
            template,
        ]
    )
    assert rc == 0
    assert len(spy.calls) == 1
    trends_url = spy.calls[0]["kwargs"]["trends_view_url"]
    assert trends_url is not None
    assert trends_url.startswith("https://gh.com/r/blob/main/trends/")
    assert trends_url.endswith("/scan.md")
    # The {date} segment must match the actual hour-dir we wrote.
    files = list((tmp_path / "trends").rglob("scan.md"))
    assert len(files) == 1
    expected_date = files[0].parent.name
    assert f"/trends/{expected_date}/scan.md" in trends_url


def test_main_notify_lark_quiet_suppresses_lark_print(
    monkeypatch, tmp_path, capsys
) -> None:
    """``--quiet`` should suppress both the success and failure [lark] prints."""
    _stub_run_scan_io(monkeypatch)
    monkeypatch.setattr(
        scan_hotspots, "default_run_command", lambda cmd, cwd=None: _fake_gh_run(cmd, cwd)
    )
    spy = _NotifySpy(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github",
            "--output-dir",
            str(tmp_path / "trends"),
            "--root",
            str(tmp_path),
            "--notify-lark",
            "--quiet",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "[lark]" not in captured.out
    assert "[lark]" not in captured.err


# --------------------------------------------------------------------------- #
# --auto-promote integration on the CLI
# --------------------------------------------------------------------------- #
def _make_args(**overrides: Any):
    """Build a Namespace mirroring ``register_scan_args`` defaults for promote.

    We only need the auto-promote-relevant + ``quiet`` fields; helpers under
    test exclusively read these via ``getattr(..., default)``.
    """
    import argparse

    base = {
        "auto_promote": False,
        "auto_promote_apply": False,
        "auto_promote_max": 1,
        "auto_promote_min_engagement": 150,
        "auto_promote_owner": "agentflow-auto",
        "auto_promote_baseline_window": 14,
        "quiet": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_trends_diff(
    monkeypatch,
    *,
    history: list[dict] | None = None,
    new_entries: list[dict] | None = None,
    promote_results: list[dict] | None = None,
    raise_in_history: BaseException | None = None,
) -> dict:
    """Monkeypatch the three trends_diff entry points used by promote.

    Returns a ``dict`` of capture lists (``history_calls``, ``detect_calls``,
    ``promote_calls``) so individual tests can assert call counts / kwargs.
    """
    captures: dict[str, list] = {
        "history_calls": [],
        "detect_calls": [],
        "promote_calls": [],
    }

    def fake_load_scan_history(trends_dir, *, limit=None):
        captures["history_calls"].append({"trends_dir": trends_dir, "limit": limit})
        if raise_in_history is not None:
            raise raise_in_history
        return list(history or [])

    def fake_detect_new_entries(latest, baseline_window, *, min_engagement=50):
        captures["detect_calls"].append(
            {
                "latest": latest,
                "baseline_window": baseline_window,
                "min_engagement": min_engagement,
            }
        )
        return list(new_entries or [])

    promote_iter = iter(promote_results or [])

    def fake_promote_to_case(
        entry,
        *,
        root,
        owner="",
        project_shape="data_pipeline",
        dry_run=True,
        scaffold_callable=None,
    ):
        captures["promote_calls"].append(
            {
                "entry": entry,
                "root": root,
                "owner": owner,
                "project_shape": project_shape,
                "dry_run": dry_run,
            }
        )
        try:
            return next(promote_iter)
        except StopIteration:
            return {
                "hotspot_id": "HSP-AUTO",
                "hotspot_name": entry.get("title") or entry.get("name") or "?",
                "case_dir": "" if dry_run else "/fake/case/dir",
                "argv": [
                    "--root", str(root),
                    "--hotspot-name", entry.get("title") or entry.get("name") or "x",
                ],
                "dry_run": dry_run,
                "returncode": None,
            }

    monkeypatch.setattr(trends_diff, "load_scan_history", fake_load_scan_history)
    monkeypatch.setattr(trends_diff, "detect_new_entries", fake_detect_new_entries)
    monkeypatch.setattr(trends_diff, "promote_to_case", fake_promote_to_case)
    return captures


def test_maybe_auto_promote_no_flag_returns_empty_and_skips_trends_diff(
    monkeypatch, tmp_path, capsys
) -> None:
    """Without ``--auto-promote``, the helper must be a no-op."""
    captures = _patch_trends_diff(monkeypatch)
    args = _make_args(auto_promote=False)

    result = _maybe_auto_promote(args, {"top": []}, tmp_path)

    assert result == []
    assert captures["history_calls"] == []
    assert captures["detect_calls"] == []
    assert captures["promote_calls"] == []
    out = capsys.readouterr().out
    assert "[promote]" not in out


def test_maybe_auto_promote_insufficient_history_returns_empty_with_msg(
    monkeypatch, tmp_path, capsys
) -> None:
    """1-scan history is not enough to define 'new' — must skip with message."""
    history = [{"path": tmp_path / "a", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}}]
    captures = _patch_trends_diff(monkeypatch, history=history)
    args = _make_args(auto_promote=True)

    result = _maybe_auto_promote(args, {"top": []}, tmp_path)

    assert result == []
    # detect/promote must NOT be called when history is too thin.
    assert captures["detect_calls"] == []
    assert captures["promote_calls"] == []
    out = capsys.readouterr().out
    assert "need >=2" in out


def test_maybe_auto_promote_no_entries_above_threshold_returns_empty(
    monkeypatch, tmp_path, capsys
) -> None:
    """detect_new_entries returns nothing -> helper returns [] cleanly."""
    history = [
        {"path": tmp_path / "a", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}},
        {"path": tmp_path / "b", "scanned_at": "2026-05-01-09", "scan_data": {"top": []}},
    ]
    captures = _patch_trends_diff(monkeypatch, history=history, new_entries=[])
    args = _make_args(auto_promote=True, auto_promote_min_engagement=200)

    result = _maybe_auto_promote(args, {}, tmp_path)

    assert result == []
    # detect was called with the threshold from args.
    assert len(captures["detect_calls"]) == 1
    assert captures["detect_calls"][0]["min_engagement"] == 200
    assert captures["promote_calls"] == []
    out = capsys.readouterr().out
    assert "no new entries above engagement=200" in out


def test_maybe_auto_promote_dry_run_calls_promote_with_dry_run_true(
    monkeypatch, tmp_path, capsys
) -> None:
    """Default ``--auto-promote`` (no apply) must call promote_to_case dry."""
    history = [
        {"path": tmp_path / "a", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}},
        {"path": tmp_path / "b", "scanned_at": "2026-05-01-09", "scan_data": {"top": []}},
    ]
    new_entries = [
        {
            "title": "alice/hot-repo",
            "url": "https://github.com/alice/hot-repo",
            "engagement": 250,
        }
    ]
    captures = _patch_trends_diff(
        monkeypatch, history=history, new_entries=new_entries
    )
    args = _make_args(auto_promote=True, auto_promote_apply=False)

    result = _maybe_auto_promote(args, {}, tmp_path)

    assert len(result) == 1
    promoted = result[0]
    assert promoted["hotspot_name"] == "alice/hot-repo"
    assert promoted["source_url"] == "https://github.com/alice/hot-repo"
    # In dry-run mode the synthesized case_dir is a placeholder — never a real path.
    assert promoted["case_dir"].startswith("<dry-run") or promoted["case_dir"] == ""

    assert len(captures["promote_calls"]) == 1
    pcall = captures["promote_calls"][0]
    assert pcall["dry_run"] is True
    assert pcall["owner"] == "agentflow-auto"
    assert pcall["project_shape"] == "data_pipeline"

    out = capsys.readouterr().out
    assert "would-create (dry-run)" in out
    assert "alice/hot-repo" in out


def test_maybe_auto_promote_apply_passes_dry_run_false(
    monkeypatch, tmp_path, capsys
) -> None:
    """``--auto-promote-apply`` must flip dry_run to False on promote_to_case."""
    history = [
        {"path": tmp_path / "a", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}},
        {"path": tmp_path / "b", "scanned_at": "2026-05-01-09", "scan_data": {"top": []}},
    ]
    new_entries = [
        {
            "title": "bob/new-tool",
            "url": "https://github.com/bob/new-tool",
            "engagement": 500,
        }
    ]
    promote_results = [
        {
            "hotspot_id": "HSP-042",
            "hotspot_name": "bob/new-tool",
            "case_dir": str(tmp_path / "cases" / "HSP-042-x"),
            "dry_run": False,
            "returncode": 0,
        }
    ]
    captures = _patch_trends_diff(
        monkeypatch,
        history=history,
        new_entries=new_entries,
        promote_results=promote_results,
    )
    args = _make_args(
        auto_promote=True,
        auto_promote_apply=True,
        auto_promote_owner="alice",
    )

    result = _maybe_auto_promote(args, {}, tmp_path)

    assert len(result) == 1
    assert result[0]["hotspot_id"] == "HSP-042"
    assert result[0]["case_dir"] == str(tmp_path / "cases" / "HSP-042-x")
    assert captures["promote_calls"][0]["dry_run"] is False
    assert captures["promote_calls"][0]["owner"] == "alice"
    out = capsys.readouterr().out
    assert "[promote] created:" in out
    assert "HSP-042" in out


def test_maybe_auto_promote_filters_out_already_shipped_urls(
    monkeypatch, tmp_path, capsys
) -> None:
    """Don't re-promote a hotspot whose URL is already a shipped case repo."""
    # Set up a shipped case under <root>/cases so discover_shipped_repos finds it.
    _write_case(
        tmp_path,
        case_dir="HSP-001-2026-04-29-already",
        hotspot_id="HSP-001",
        final_status="publish",
        owner="alice",
        repo_name="already-shipped",
    )

    history = [
        {"path": tmp_path / "a", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}},
        {"path": tmp_path / "b", "scanned_at": "2026-05-01-09", "scan_data": {"top": []}},
    ]
    new_entries = [
        # Already shipped — must be filtered out.
        {
            "title": "alice/already-shipped",
            "url": "https://github.com/alice/already-shipped",
            "engagement": 999,
        },
        # New — must be promoted.
        {
            "title": "carol/fresh-thing",
            "url": "https://github.com/carol/fresh-thing",
            "engagement": 200,
        },
    ]
    captures = _patch_trends_diff(
        monkeypatch, history=history, new_entries=new_entries
    )
    # max=5 so the cap doesn't mask the filter behaviour.
    args = _make_args(auto_promote=True, auto_promote_max=5)

    result = _maybe_auto_promote(args, {}, tmp_path)

    assert len(result) == 1
    assert result[0]["source_url"] == "https://github.com/carol/fresh-thing"
    # promote_to_case must have been called exactly once — for the unfiltered entry.
    assert len(captures["promote_calls"]) == 1
    assert (
        captures["promote_calls"][0]["entry"]["url"]
        == "https://github.com/carol/fresh-thing"
    )


def test_maybe_auto_promote_caps_at_auto_promote_max(
    monkeypatch, tmp_path, capsys
) -> None:
    """detect returns 5 entries; --auto-promote-max=1 must promote only 1."""
    history = [
        {"path": tmp_path / "a", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}},
        {"path": tmp_path / "b", "scanned_at": "2026-05-01-09", "scan_data": {"top": []}},
    ]
    new_entries = [
        {
            "title": f"owner/repo-{i}",
            "url": f"https://github.com/owner/repo-{i}",
            "engagement": 300 + i,
        }
        for i in range(5)
    ]
    captures = _patch_trends_diff(
        monkeypatch, history=history, new_entries=new_entries
    )
    args = _make_args(auto_promote=True, auto_promote_max=1)

    result = _maybe_auto_promote(args, {}, tmp_path)

    assert len(result) == 1
    # Order from detect_new_entries is preserved — first entry wins.
    assert result[0]["source_url"] == "https://github.com/owner/repo-0"
    assert len(captures["promote_calls"]) == 1


def test_maybe_auto_promote_swallows_trends_diff_exception(
    monkeypatch, tmp_path, capsys
) -> None:
    """A trends_diff failure must NEVER propagate — scan must keep exit 0."""
    _patch_trends_diff(
        monkeypatch, raise_in_history=RuntimeError("disk on fire")
    )
    args = _make_args(auto_promote=True)

    result = _maybe_auto_promote(args, {}, tmp_path)

    assert result == []
    captured = capsys.readouterr()
    assert "[promote] failed (caught" in captured.err
    assert "disk on fire" in captured.err


def test_maybe_notify_lark_forwards_promoted_cases_kwarg(
    monkeypatch, tmp_path, capsys
) -> None:
    """``promoted_cases`` arg must reach notify_scan_complete as auto_promoted_cases."""
    spy = _NotifySpy()
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    sentinel_cases = [
        {
            "hotspot_id": "HSP-AUTO-1",
            "hotspot_name": "carol/fresh-thing",
            "case_dir": str(tmp_path / "cases" / "HSP-AUTO-1"),
            "source_url": "https://github.com/carol/fresh-thing",
        }
    ]
    args = _make_args(
        notify_lark=True,
        lark_dry_run=True,
        lark_framework_repo_url="https://example.com/framework",
        lark_trends_view_url_template="",
        root=str(tmp_path),
    )
    aggregate = {
        "scanned_at": "2026-05-01T10:00:00+00:00",
        "unique_count": 0,
        "by_source": {"github": 0, "hackernews": 0, "reddit": 0},
        "top": [],
        "output_dir": str(tmp_path / "trends" / "2026-05-01-10"),
    }

    _maybe_notify_lark(args, aggregate, tmp_path, promoted_cases=sentinel_cases)

    assert len(spy.calls) == 1
    call_kwargs = spy.calls[0]["kwargs"]
    assert "auto_promoted_cases" in call_kwargs
    assert call_kwargs["auto_promoted_cases"] == sentinel_cases
    # And without an explicit promoted_cases arg, it defaults to [] (not omitted).
    spy2 = _NotifySpy()
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy2)
    _maybe_notify_lark(args, aggregate, tmp_path)
    assert spy2.calls[0]["kwargs"]["auto_promoted_cases"] == []


def test_main_auto_promote_end_to_end_passes_promoted_to_lark(
    monkeypatch, tmp_path, capsys
) -> None:
    """Full main() wiring: --auto-promote + --notify-lark must thread the cases through."""
    _stub_run_scan_io(monkeypatch)
    monkeypatch.setattr(
        scan_hotspots,
        "default_run_command",
        lambda cmd, cwd=None: _fake_gh_run(cmd, cwd),
    )

    # Make trends_diff produce one new entry that gets dry-run promoted.
    new_entries = [
        {
            "title": "alice/demo",
            "url": "https://github.com/alice/demo",
            "engagement": 999,
        }
    ]
    # We need at least 2 history entries -> patch load_scan_history to return 2.
    fake_history = [
        {"path": tmp_path / "x", "scanned_at": "2026-05-01-10", "scan_data": {"top": []}},
        {"path": tmp_path / "y", "scanned_at": "2026-05-01-09", "scan_data": {"top": []}},
    ]
    _patch_trends_diff(
        monkeypatch, history=fake_history, new_entries=new_entries
    )

    spy = _NotifySpy()
    monkeypatch.setattr(scan_hotspots, "_notify_scan_complete", spy)

    rc = main(
        [
            "--queries",
            "solana ai agent",
            "--sources",
            "github",
            "--output-dir",
            str(tmp_path / "trends"),
            "--root",
            str(tmp_path),
            "--auto-promote",
            "--notify-lark",
            "--lark-dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # promote line surfaced.
    assert "[promote] would-create (dry-run)" in out
    # and Lark spy got the promoted cases
    assert len(spy.calls) == 1
    promoted = spy.calls[0]["kwargs"]["auto_promoted_cases"]
    assert isinstance(promoted, list)
    assert len(promoted) == 1
    assert promoted[0]["source_url"] == "https://github.com/alice/demo"
