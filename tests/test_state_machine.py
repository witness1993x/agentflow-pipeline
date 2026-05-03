"""Tests for the publish-readiness / kill-signal state machine in run_pipeline."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentflow_pipeline import cli as rp


# ---------------------------------------------------------------------------
# compute_next_review_date
# ---------------------------------------------------------------------------

class TestComputeNextReviewDate:
    @staticmethod
    def _days_between(today_iso: str, target_iso: str) -> int:
        today = datetime.strptime(today_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        target = datetime.strptime(target_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (target - today).days

    def test_passed_returns_seven_days(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = rp.compute_next_review_date("passed")
        assert self._days_between(today, result) == 7

    def test_blocked_returns_three_days(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = rp.compute_next_review_date("blocked")
        assert self._days_between(today, result) == 3

    def test_failed_returns_fourteen_days(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = rp.compute_next_review_date("failed")
        assert self._days_between(today, result) == 14

    def test_unknown_outcome_uses_default(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = rp.compute_next_review_date("not-a-real-status", default_days=10)
        assert self._days_between(today, result) == 10


# ---------------------------------------------------------------------------
# detect_kill_signal_triggers
# ---------------------------------------------------------------------------

class TestDetectKillSignalTriggers:
    def test_substring_match_in_stderr(self) -> None:
        config = {"gate_4_buildability": {"kill_signals": ["timeout"]}}
        results = {
            "build": {"status": "failed", "stderr": "Build hit a timeout after 5 minutes", "stdout": ""},
        }
        triggered = rp.detect_kill_signal_triggers(config, results)
        assert triggered == ["timeout"]

    def test_case_insensitive(self) -> None:
        config = {"gate_4_buildability": {"kill_signals": ["TIMEOUT"]}}
        results = {
            "test": {"status": "failed", "stderr": "process exited due to timeout", "stdout": ""},
        }
        triggered = rp.detect_kill_signal_triggers(config, results)
        assert triggered == ["TIMEOUT"]

    def test_only_failed_steps_inspected(self) -> None:
        """A passed step containing the signal text must NOT trigger."""
        config = {"gate_4_buildability": {"kill_signals": ["timeout"]}}
        results = {
            "install": {"status": "passed", "stderr": "no timeout occurred", "stdout": ""},
            "build": {"status": "passed", "stderr": "", "stdout": "timeout warning ignored"},
        }
        triggered = rp.detect_kill_signal_triggers(config, results)
        assert triggered == []

    def test_no_signals_configured_returns_empty(self) -> None:
        config = {"gate_4_buildability": {}}
        results = {"build": {"status": "failed", "stderr": "boom"}}
        assert rp.detect_kill_signal_triggers(config, results) == []

    def test_dedup_when_multiple_failed_steps_share_signal(self) -> None:
        config = {"gate_4_buildability": {"kill_signals": ["panic"]}}
        results = {
            "build": {"status": "failed", "stderr": "rust panic at line 5"},
            "test": {"status": "failed", "stderr": "panic was thrown"},
        }
        triggered = rp.detect_kill_signal_triggers(config, results)
        assert triggered == ["panic"]


# ---------------------------------------------------------------------------
# evaluate_publish_readiness
# ---------------------------------------------------------------------------

def _readiness_config(**overrides) -> dict:
    """Skeleton config that defaults to "not_started" but is easily mutated."""
    cfg: dict = {
        "execution_state": {
            "data_probe": {"status": ""},
            "probe": {"install_status": "", "build_status": "", "test_status": ""},
            "kafka_probe": {"status": ""},
            "publish": {"publish_status": ""},
        },
        "gate_4_buildability": {"verdict": ""},
        "pre_build_analysis": {"chainstream_fit": {"verdict": "", "target_capability": "graphql"}},
    }
    for path, value in overrides.items():
        # Path is dot-delimited.
        keys = path.split(".")
        cursor = cfg
        for k in keys[:-1]:
            cursor = cursor.setdefault(k, {})
        cursor[keys[-1]] = value
    return cfg


class TestEvaluatePublishReadiness:
    def test_not_started(self) -> None:
        cfg = _readiness_config()
        assert rp.evaluate_publish_readiness(cfg) == "not_started"
        assert cfg["execution_state"]["publish_readiness"]["status"] == "not_started"

    def test_in_progress(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.data_probe.status": "passed",
            "execution_state.probe.install_status": "passed",
            # build/test left blank → not all probes done, fit not pass yet
        })
        assert rp.evaluate_publish_readiness(cfg) == "in_progress"

    def test_blocked_data_probe(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.data_probe.status": "failed",
        })
        assert rp.evaluate_publish_readiness(cfg) == "blocked_data_probe"

    def test_blocked_buildability(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.probe.install_status": "passed",
            "execution_state.probe.build_status": "failed",
            "execution_state.probe.test_status": "passed",
        })
        assert rp.evaluate_publish_readiness(cfg) == "blocked_buildability"

    def test_blocked_kafka_probe(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.data_probe.status": "passed",
            "execution_state.probe.install_status": "passed",
            "execution_state.probe.build_status": "passed",
            "execution_state.probe.test_status": "passed",
            "execution_state.kafka_probe.status": "failed",
            "gate_4_buildability.verdict": "pass",
            "pre_build_analysis.chainstream_fit.verdict": "pass",
            "pre_build_analysis.chainstream_fit.target_capability": "kafka",
        })
        assert rp.evaluate_publish_readiness(cfg) == "blocked_kafka_probe"

    def test_ready(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.data_probe.status": "passed",
            "execution_state.probe.install_status": "passed",
            "execution_state.probe.build_status": "passed",
            "execution_state.probe.test_status": "passed",
            "gate_4_buildability.verdict": "pass",
            "pre_build_analysis.chainstream_fit.verdict": "pass",
            "pre_build_analysis.chainstream_fit.target_capability": "graphql",
        })
        assert rp.evaluate_publish_readiness(cfg) == "ready"

    def test_ready_when_kafka_required_and_passed(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.data_probe.status": "passed",
            "execution_state.probe.install_status": "passed",
            "execution_state.probe.build_status": "passed",
            "execution_state.probe.test_status": "passed",
            "execution_state.kafka_probe.status": "passed",
            "gate_4_buildability.verdict": "pass",
            "pre_build_analysis.chainstream_fit.verdict": "pass",
            "pre_build_analysis.chainstream_fit.target_capability": "kafka",
        })
        assert rp.evaluate_publish_readiness(cfg) == "ready"

    def test_published(self) -> None:
        cfg = _readiness_config(**{
            "execution_state.publish.publish_status": "passed",
        })
        assert rp.evaluate_publish_readiness(cfg) == "published"
