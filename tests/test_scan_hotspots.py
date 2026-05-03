"""Tests for ``agentflow_pipeline.scan_hotspots`` (the agentflow-scan CLI)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentflow_pipeline import scan_hotspots
from agentflow_pipeline.scan_hotspots import (
    ALL_SOURCES,
    DEFAULT_QUERIES,
    DEFAULT_SOURCES,
    aggregate_scan_results,
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
