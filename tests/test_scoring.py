"""Tests for the candidate scoring helpers in run_pipeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentflow_pipeline import cli as rp


def _base_config(hotspot_name: str = "solana dex monitor", shape: str = "indexer") -> dict:
    return {
        "meta": {"hotspot_name": hotspot_name},
        "gate_2_project_shape": {"project_shape": shape},
    }


def _iso_days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    def test_archived_repo_strongly_penalised(self) -> None:
        config = _base_config()
        candidate = {
            "name": "solana-dex-monitor",
            "description": "indexer for solana dex trades",
            "stars": 1500,
            "is_archived": True,
            "license_note": "MIT",
            "language": "python",
        }
        score, reason, signals = rp.score_candidate(config, candidate)
        assert signals["archived_penalty"] == -40
        assert "已归档" in reason
        # An archived repo should not outrank a healthy one of the same shape.
        candidate2 = dict(candidate)
        candidate2["is_archived"] = False
        score2, _, _ = rp.score_candidate(config, candidate2)
        assert score2 > score

    def test_pushed_at_takes_priority_over_updated_at(self) -> None:
        """When pushed_at is recent but updated_at is old, the recent activity wins."""
        config = _base_config()
        candidate = {
            "name": "indexer-foo",
            "description": "indexer",
            "stars": 50,
            "pushed_at": _iso_days_ago(5),
            "updated_at": _iso_days_ago(800),
        }
        _, reason, signals = rp.score_candidate(config, candidate)
        # 5 days falls into the <=14 day bucket worth +18.
        assert signals["activity_score"] == 18
        assert signals["activity_age_days"] <= 14
        assert "两周内" in reason

    def test_friendly_language_adds_bonus(self) -> None:
        config = _base_config()
        candidate_py = {"name": "thing", "description": "x", "language": "Python"}
        candidate_other = {"name": "thing", "description": "x", "language": "Brainfuck"}
        _, _, signals_py = rp.score_candidate(config, candidate_py)
        _, _, signals_other = rp.score_candidate(config, candidate_other)
        assert signals_py.get("language_score") == 6
        assert "language_score" not in signals_other

    def test_empty_config_does_not_crash(self) -> None:
        """Even with a completely empty config dict the helper must not raise."""
        empty: dict = {}
        candidate = {"name": "anything", "description": ""}
        score, reason, signals = rp.score_candidate(empty, candidate)
        assert isinstance(score, int)
        assert isinstance(reason, str) and reason
        assert isinstance(signals, dict)
        assert "total_score" in signals


# ---------------------------------------------------------------------------
# assess_chainstream_fit
# ---------------------------------------------------------------------------

class TestAssessChainstreamFit:
    def test_graphql_keyword_sets_access_method_graphql(self) -> None:
        candidate = {
            "name": "chain-graphql-explorer",
            "description": "GraphQL gateway for on-chain data",
            "language": "typescript",
        }
        score, reason, access = rp.assess_chainstream_fit(candidate)
        assert access == "graphql"
        assert score > 0
        assert "graphql" in reason.lower() or "GraphQL" in reason

    def test_kafka_keyword_sets_access_method_kafka(self) -> None:
        candidate = {
            "name": "kafka-streams-defi",
            "description": "Kafka streams pipeline for DeFi events",
            "language": "java",
        }
        _, _, access = rp.assess_chainstream_fit(candidate)
        assert access == "kafka"

    def test_archived_clamps_score_down(self) -> None:
        candidate = {
            "name": "graphql-defi",
            "description": "graphql kafka solana ethereum dex analytics",
            "language": "python",
            "is_archived": True,
        }
        score, _, _ = rp.assess_chainstream_fit(candidate)
        # Archived clamps score down by 25, but score is also capped at 100.
        assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# recommend_fork_or_build
# ---------------------------------------------------------------------------

class TestRecommendForkOrBuild:
    def test_archived_recommends_build_new(self) -> None:
        candidate = {"is_archived": True, "score": 80, "chainstream_fit_score": 80, "license_note": "MIT"}
        recommendation, reason = rp.recommend_fork_or_build(candidate)
        assert recommendation == "build_new"
        assert "归档" in reason

    def test_high_fit_friendly_license_recommends_fork(self) -> None:
        candidate = {
            "is_archived": False,
            "score": 60,
            "chainstream_fit_score": 75,
            "license_note": "MIT",
        }
        recommendation, _ = rp.recommend_fork_or_build(candidate)
        assert recommendation == "fork_existing"

    def test_mid_fit_recommends_template_clone(self) -> None:
        candidate = {
            "is_archived": False,
            "score": 30,
            "chainstream_fit_score": 50,
            "license_note": "GPL-3.0",
        }
        recommendation, _ = rp.recommend_fork_or_build(candidate)
        assert recommendation == "template_clone"

    def test_low_fit_recommends_build_new(self) -> None:
        candidate = {
            "is_archived": False,
            "score": 5,
            "chainstream_fit_score": 5,
            "license_note": "",
        }
        recommendation, _ = rp.recommend_fork_or_build(candidate)
        assert recommendation == "build_new"
