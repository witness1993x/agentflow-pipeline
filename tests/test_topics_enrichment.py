"""Tests for topics_enrichment helpers."""
from __future__ import annotations

from typing import Any, Callable, List

import pytest

from agentflow_pipeline.topics_enrichment import (
    enrich_candidates_with_topics,
    fetch_repo_topics,
    parse_repo_owner_name,
)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# parse_repo_owner_name
# ---------------------------------------------------------------------------

class TestParseRepoOwnerName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("octo/repo", ("octo", "repo")),
            ("  octo/repo  ", ("octo", "repo")),
            ("https://github.com/octo/repo", ("octo", "repo")),
            ("https://github.com/octo/repo.git", ("octo", "repo")),
            ("git@github.com:octo/repo.git", ("octo", "repo")),
            ("octo/repo/extra", ("octo", "repo")),
            ("/leading/slash", ("leading", "slash")),
        ],
    )
    def test_valid_inputs(self, raw: str, expected: tuple[str, str]) -> None:
        assert parse_repo_owner_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "nope", "/", " ", "/onepart"])
    def test_invalid_inputs_return_blank_pair(self, raw: str) -> None:
        assert parse_repo_owner_name(raw) == ("", "")


# ---------------------------------------------------------------------------
# fetch_repo_topics
# ---------------------------------------------------------------------------

class TestFetchRepoTopics:
    def test_success_parses_json_array(self) -> None:
        def runner(cmd, cwd=None):
            return _FakeCompleted(0, '["solana", "dex"]')
        assert fetch_repo_topics("foo", "bar", runner) == ["solana", "dex"]

    def test_run_command_exception_returns_empty(self) -> None:
        def runner(cmd, cwd=None):
            raise RuntimeError("subprocess died")
        assert fetch_repo_topics("foo", "bar", runner) == []

    def test_non_zero_returncode_returns_empty(self) -> None:
        def runner(cmd, cwd=None):
            return _FakeCompleted(1, "", "boom")
        assert fetch_repo_topics("foo", "bar", runner) == []

    def test_bad_json_returns_empty(self) -> None:
        def runner(cmd, cwd=None):
            return _FakeCompleted(0, "not json at all")
        assert fetch_repo_topics("foo", "bar", runner) == []

    def test_blank_owner_returns_empty_without_invocation(self) -> None:
        called: list = []

        def runner(cmd, cwd=None):
            called.append(cmd)
            return _FakeCompleted(0, "[]")

        assert fetch_repo_topics("", "bar", runner) == []
        assert called == []

    def test_lowercases_and_strips_topics(self) -> None:
        def runner(cmd, cwd=None):
            return _FakeCompleted(0, '["  Solana  ", "DEX", ""]')
        assert fetch_repo_topics("a", "b", runner) == ["solana", "dex"]


# ---------------------------------------------------------------------------
# enrich_candidates_with_topics
# ---------------------------------------------------------------------------

def _runner_returning(map_: dict) -> Callable[..., _FakeCompleted]:
    def runner(cmd, cwd=None):
        target = cmd[2] if len(cmd) > 2 else ""
        if target in map_:
            return _FakeCompleted(0, map_[target])
        return _FakeCompleted(1, "", "not found")
    return runner


class TestEnrichCandidatesWithTopics:
    def test_caps_bonus_at_25_per_candidate(self) -> None:
        # 5 keywords matched should already hit the +25 cap; a 6th still caps.
        canned = {
            "repos/carol/indexer": (
                '["indexer","graphql","onchain","ethereum","defi","blockchain"]'
            ),
        }
        candidates = [
            {"source": "github_search", "name": "carol/indexer", "chainstream_fit_score": 25},
        ]
        runner = _runner_returning(canned)
        stats = enrich_candidates_with_topics(candidates, runner, max_calls=5)
        assert stats["calls_made"] == 1
        assert stats["candidates_enriched"] == 1
        assert candidates[0]["chainstream_fit_score"] == 25 + 25  # capped

    def test_max_calls_2_only_runs_first_two(self) -> None:
        canned = {
            "repos/alice/repo": '["solana","dex"]',
            "repos/bob/repo": '["kafka"]',
            "repos/carol/repo": '["graphql"]',
        }
        candidates = [
            {"source": "github_search", "name": "alice/repo", "chainstream_fit_score": 0},
            {"source": "github_search", "name": "bob/repo", "chainstream_fit_score": 0},
            {"source": "github_search", "name": "carol/repo", "chainstream_fit_score": 0},
        ]
        runner = _runner_returning(canned)
        stats = enrich_candidates_with_topics(candidates, runner, max_calls=2)
        assert stats["calls_made"] == 2
        # carol was never visited
        assert "topics" not in candidates[2]
        assert candidates[0]["topics"] == ["solana", "dex"]
        assert candidates[1]["topics"] == ["kafka"]

    def test_non_github_source_is_skipped(self) -> None:
        candidates = [
            {"source": "jina_search", "name": "alice/x", "chainstream_fit_score": 5},
        ]

        def runner(cmd, cwd=None):  # should never be called
            raise AssertionError("non-github candidates must not trigger gh api")

        stats = enrich_candidates_with_topics(candidates, runner, max_calls=3)
        assert stats == {"calls_made": 0, "topics_found_total": 0, "candidates_enriched": 0}
        assert "topics" not in candidates[0]

    def test_zero_max_calls_is_no_op(self) -> None:
        candidates = [
            {"source": "github_search", "name": "alice/x", "chainstream_fit_score": 5},
        ]

        def runner(cmd, cwd=None):
            raise AssertionError("max_calls=0 must short-circuit")

        stats = enrich_candidates_with_topics(candidates, runner, max_calls=0)
        assert stats == {"calls_made": 0, "topics_found_total": 0, "candidates_enriched": 0}
