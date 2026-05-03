"""Tests for the fail-closed auto_publish safety gate."""
from __future__ import annotations

from copy import deepcopy

import pytest

from agentflow_pipeline.auto_publish import auto_publish_dry_run, check_auto_publish_safety


# ---------------------------------------------------------------------------
# check_auto_publish_safety
# ---------------------------------------------------------------------------

class TestCheckAutoPublishSafety:
    def test_ready_config_passes(self, ready_config) -> None:
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is True, blockers
        assert blockers == []

    def test_missing_owner_blocked(self, ready_config) -> None:
        ready_config["repo_plan"]["github_owner"] = ""
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("github_owner" in b for b in blockers)

    def test_missing_repo_name_blocked(self, ready_config) -> None:
        ready_config["repo_plan"]["repo_name"] = ""
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("repo_name" in b for b in blockers)

    def test_already_published_blocked(self, ready_config) -> None:
        """Idempotency: 'passed' must short-circuit even if everything else is fine."""
        ready_config["execution_state"]["publish"]["publish_status"] = "passed"
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("publish_status" in b and "passed" in b for b in blockers)

    def test_veto_from_gate_blocked(self, ready_config) -> None:
        ready_config["decision"]["veto_from_gate"] = "gate_4_buildability"
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("veto_from_gate" in b for b in blockers)

    def test_kill_signals_triggered_blocked(self, ready_config) -> None:
        ready_config["gate_4_buildability"]["kill_signals_triggered"] = ["build_timeout"]
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("kill_signals_triggered" in b for b in blockers)

    def test_chainstream_fit_not_pass_blocked(self, ready_config) -> None:
        ready_config["pre_build_analysis"]["chainstream_fit"]["verdict"] = "hold"
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("chainstream_fit" in b for b in blockers)

    def test_blocked_config_has_multiple_blockers(self, blocked_config) -> None:
        ok, blockers = check_auto_publish_safety(blocked_config)
        assert ok is False
        assert len(blockers) >= 3, blockers

    def test_undecided_strategy_blocked(self, ready_config) -> None:
        ready_config["gate_3_repo_routing"]["repo_strategy"] = "undecided"
        ok, blockers = check_auto_publish_safety(ready_config)
        assert ok is False
        assert any("repo_strategy" in b for b in blockers)


# ---------------------------------------------------------------------------
# auto_publish_dry_run
# ---------------------------------------------------------------------------

class TestAutoPublishDryRun:
    def test_dry_run_for_ready(self, ready_config) -> None:
        preview = auto_publish_dry_run(ready_config)
        assert preview["would_publish"] is True
        assert preview["readiness"] == "ready"
        assert preview["planned_repo_ref"] == "example-org/demo-hotspot"
        assert preview["planned_strategy"] == "fork_existing"
        assert preview["blockers"] == []

    def test_dry_run_for_blocked(self, blocked_config) -> None:
        preview = auto_publish_dry_run(blocked_config)
        assert preview["would_publish"] is False
        # planned_repo_ref ends up empty because owner is blank
        assert preview["planned_repo_ref"] == ""
        assert preview["blockers"]

    def test_dry_run_does_not_mutate_input(self, ready_config) -> None:
        before = deepcopy(ready_config)
        auto_publish_dry_run(ready_config)
        assert ready_config == before
