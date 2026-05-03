"""Tests for ``pool_advancer`` -- per-case readiness-driven mode picker."""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agentflow_pipeline import pool_advancer
from agentflow_pipeline.pool_advancer import (
    describe_advance_decision,
    format_advance_summary,
    next_mode_for,
    register_advance_args,
    run_pool_auto_advance,
)


# --------------------------------------------------------------------------- #
# Config builders
# --------------------------------------------------------------------------- #
def _empty_config() -> dict:
    """A bare config: nothing has been run, nothing has been decided."""
    return {
        "decision": {"final_status": "draft"},
        "gate_3_repo_routing": {"repo_strategy": "undecided"},
        "execution_state": {
            "discovery": {"last_run_at": ""},
            "data_probe": {"status": ""},
            "probe": {"last_run_at": ""},
            "publish_readiness": {"status": "not_started"},
        },
    }


def _config_after_discover() -> dict:
    cfg = _empty_config()
    cfg["execution_state"]["discovery"]["last_run_at"] = "2026-05-01T00:00:00Z"
    cfg["gate_3_repo_routing"]["repo_strategy"] = "fork_existing"
    cfg["execution_state"]["publish_readiness"]["status"] = "in_progress"
    return cfg


def _config_after_data_probe_pass() -> dict:
    cfg = _config_after_discover()
    cfg["execution_state"]["data_probe"]["status"] = "passed"
    return cfg


def _write_gate(case_dir: Path, config: dict) -> Path:
    """Persist ``config`` to ``case_dir/02-pipeline-gate.yaml``."""
    import yaml  # local import keeps top-level test importable without pyyaml

    case_dir.mkdir(parents=True, exist_ok=True)
    gate = case_dir / "02-pipeline-gate.yaml"
    gate.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return gate


# --------------------------------------------------------------------------- #
# next_mode_for: direct decision-tree coverage
# --------------------------------------------------------------------------- #
def test_empty_config_returns_discover() -> None:
    assert next_mode_for(_empty_config()) == "discover"


def test_after_discover_with_strategy_returns_data_probe() -> None:
    cfg = _config_after_discover()
    assert next_mode_for(cfg) == "data-probe"


def test_after_discover_but_strategy_undecided_returns_none() -> None:
    cfg = _config_after_discover()
    cfg["gate_3_repo_routing"]["repo_strategy"] = "undecided"
    # discovery done + undecided strategy -> we shouldn't push data-probe
    assert next_mode_for(cfg) is None


def test_data_probe_passed_returns_probe() -> None:
    assert next_mode_for(_config_after_data_probe_pass()) == "probe"


def test_data_probe_not_passed_does_not_return_probe() -> None:
    cfg = _config_after_discover()
    cfg["execution_state"]["data_probe"]["status"] = "in_progress"
    # data_probe not yet passed -> not safe to spend cycles on build probe
    assert next_mode_for(cfg) is None


def test_publish_readiness_ready_returns_none_by_default() -> None:
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = "ready"
    cfg["execution_state"]["probe"]["last_run_at"] = "2026-05-01T01:00:00Z"
    assert next_mode_for(cfg) is None


def test_publish_readiness_published_returns_none() -> None:
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = "published"
    assert next_mode_for(cfg) is None


@pytest.mark.parametrize(
    "blocked",
    ["blocked_data_probe", "blocked_kafka_probe", "blocked_buildability"],
)
def test_blocked_states_return_none(blocked: str) -> None:
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = blocked
    assert next_mode_for(cfg) is None


def test_final_status_drop_returns_none() -> None:
    cfg = _empty_config()  # otherwise this case would have answered "discover"
    cfg["decision"]["final_status"] = "drop"
    assert next_mode_for(cfg) is None


def test_include_publish_flag_controls_ready_dispatch() -> None:
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = "ready"
    cfg["execution_state"]["probe"]["last_run_at"] = "2026-05-01T01:00:00Z"
    assert next_mode_for(cfg, include_publish=False) is None
    assert next_mode_for(cfg, include_publish=True) == "publish"


def test_include_publish_does_not_affect_blocked() -> None:
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = "blocked_buildability"
    assert next_mode_for(cfg, include_publish=True) is None


def test_invalid_config_returns_none() -> None:
    assert next_mode_for(None) is None  # type: ignore[arg-type]
    assert next_mode_for("not a dict") is None  # type: ignore[arg-type]
    assert next_mode_for({}) == "discover"  # empty dict still falls through


# --------------------------------------------------------------------------- #
# describe_advance_decision
# --------------------------------------------------------------------------- #
def test_describe_advance_decision_covers_all_six_states() -> None:
    """All six readiness states (plus drop) must produce non-empty zh text."""
    base = _config_after_data_probe_pass()

    cases = {
        "not_started": _empty_config(),
        "in_progress": base,  # in_progress with everything done -> probe step
        "blocked_data_probe": (lambda c: (
            c.__setitem__(
                "execution_state",
                {**c["execution_state"], "publish_readiness":
                    {"status": "blocked_data_probe"}},
            ) or c
        ))(deepcopy(base)),
        "blocked_kafka_probe": (lambda c: (
            c.__setitem__(
                "execution_state",
                {**c["execution_state"], "publish_readiness":
                    {"status": "blocked_kafka_probe"}},
            ) or c
        ))(deepcopy(base)),
        "blocked_buildability": (lambda c: (
            c.__setitem__(
                "execution_state",
                {**c["execution_state"], "publish_readiness":
                    {"status": "blocked_buildability"}},
            ) or c
        ))(deepcopy(base)),
        "ready": (lambda c: (
            c.__setitem__(
                "execution_state",
                {**c["execution_state"], "publish_readiness":
                    {"status": "ready"}},
            ) or c
        ))(deepcopy(base)),
        "published": (lambda c: (
            c.__setitem__(
                "execution_state",
                {**c["execution_state"], "publish_readiness":
                    {"status": "published"}},
            ) or c
        ))(deepcopy(base)),
    }

    for label, cfg in cases.items():
        desc = describe_advance_decision(cfg)
        assert isinstance(desc, str) and desc.strip(), (
            f"empty description for state {label}"
        )


def test_describe_includes_publish_when_flag_set() -> None:
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = "ready"
    msg_off = describe_advance_decision(cfg, include_publish=False)
    msg_on = describe_advance_decision(cfg, include_publish=True)
    assert "publish" in msg_off  # mentions waiting for human publish
    assert "publish" in msg_on


def test_describe_dropped_case() -> None:
    cfg = _empty_config()
    cfg["decision"]["final_status"] = "drop"
    assert "drop" in describe_advance_decision(cfg)


# --------------------------------------------------------------------------- #
# register_advance_args
# --------------------------------------------------------------------------- #
def test_register_advance_args_adds_three_flags() -> None:
    parser = argparse.ArgumentParser()
    register_advance_args(parser)
    args = parser.parse_args([])
    assert args.pool_auto_advance is False
    assert args.pool_auto_advance_max_rounds == 3
    assert args.pool_auto_advance_include_publish is False

    args2 = parser.parse_args([
        "--pool-auto-advance",
        "--pool-auto-advance-max-rounds", "5",
        "--pool-auto-advance-include-publish",
    ])
    assert args2.pool_auto_advance is True
    assert args2.pool_auto_advance_max_rounds == 5
    assert args2.pool_auto_advance_include_publish is True


# --------------------------------------------------------------------------- #
# run_pool_auto_advance: monkeypatch run_pool_parallel
# --------------------------------------------------------------------------- #
class _FakePoolRunner:
    """Capture run_pool_parallel calls and (optionally) mutate gate yamls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.advance_callback: Any = None

    def __call__(
        self,
        cases: list[Path],
        mode: str,
        *,
        max_workers: int = 4,
        extra_args: list[str] | None = None,
        run_pipeline_script: Path,
        timeout_per_case: int = 300,
        on_complete_callable: Any = None,
    ) -> dict:
        self.calls.append({
            "mode": mode,
            "cases": [str(c) for c in cases],
            "max_workers": max_workers,
            "extra_args": list(extra_args or []),
            "timeout_per_case": timeout_per_case,
        })
        if self.advance_callback is not None:
            for c in cases:
                self.advance_callback(Path(c), mode)
        return {
            "total": len(cases),
            "passed": len(cases),
            "failed": 0,
            "timeout": 0,
            "duration_seconds": 0.01,
            "results": [
                {"case_dir": str(c), "mode": mode, "status": "passed",
                 "returncode": 0, "stdout_tail": "", "stderr_tail": "",
                 "duration_seconds": 0.01}
                for c in cases
            ],
        }


def test_run_pool_auto_advance_groups_three_cases_by_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three cases at three different stages -> three groups in round 1."""
    case_a = tmp_path / "HSP-001-2026-05-01-a"
    case_b = tmp_path / "HSP-002-2026-05-01-b"
    case_c = tmp_path / "HSP-003-2026-05-01-c"
    _write_gate(case_a, _empty_config())  # -> discover
    _write_gate(case_b, _config_after_discover())  # -> data-probe
    _write_gate(case_c, _config_after_data_probe_pass())  # -> probe

    fake = _FakePoolRunner()
    monkeypatch.setattr(pool_advancer, "run_pool_parallel", fake)

    report = run_pool_auto_advance(
        [case_a, case_b, case_c],
        max_workers=2,
        run_pipeline_script=tmp_path / "fake_pipeline.py",
        timeout_per_case=42,
        max_rounds=1,
    )

    # Three different modes were dispatched in a single round.
    assert len(fake.calls) == 3
    modes_dispatched = {call["mode"] for call in fake.calls}
    assert modes_dispatched == {"discover", "data-probe", "probe"}

    # Each call carried exactly one case (since they each map to a distinct mode).
    for call in fake.calls:
        assert len(call["cases"]) == 1
        assert call["max_workers"] == 2
        assert call["timeout_per_case"] == 42

    assert report["rounds_completed"] == 1
    assert report["total_cases"] == 3
    assert report["advanced_cases"] == 3


def test_run_pool_auto_advance_max_rounds_caps_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When yaml never updates (fake runner), max_rounds halts the loop."""
    case = tmp_path / "HSP-001-2026-05-01-stuck"
    _write_gate(case, _empty_config())  # forever asks for 'discover'

    fake = _FakePoolRunner()
    monkeypatch.setattr(pool_advancer, "run_pool_parallel", fake)

    report = run_pool_auto_advance(
        [case],
        max_workers=1,
        run_pipeline_script=tmp_path / "fake_pipeline.py",
        max_rounds=4,
    )

    # Without state changes, every round dispatches "discover" once.
    assert report["rounds_completed"] == 4
    assert len(fake.calls) == 4
    assert all(call["mode"] == "discover" for call in fake.calls)


def test_run_pool_auto_advance_walks_two_steps_when_yaml_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutating the yaml between rounds advances the case to the next step."""
    case = tmp_path / "HSP-001-2026-05-01-walk"
    _write_gate(case, _empty_config())  # round 1 -> discover

    def _advance(p: Path, mode: str) -> None:
        if mode == "discover":
            _write_gate(p, _config_after_discover())
        elif mode == "data-probe":
            _write_gate(p, _config_after_data_probe_pass())
        elif mode == "probe":
            cfg = _config_after_data_probe_pass()
            cfg["execution_state"]["probe"]["last_run_at"] = "2026-05-01T02:00:00Z"
            cfg["execution_state"]["publish_readiness"]["status"] = "ready"
            _write_gate(p, cfg)

    fake = _FakePoolRunner()
    fake.advance_callback = _advance
    monkeypatch.setattr(pool_advancer, "run_pool_parallel", fake)

    report = run_pool_auto_advance(
        [case],
        max_workers=1,
        run_pipeline_script=tmp_path / "fake_pipeline.py",
        max_rounds=5,
    )

    # round 1: discover, round 2: data-probe, round 3: probe, round 4: stops.
    modes = [c["mode"] for c in fake.calls]
    assert modes == ["discover", "data-probe", "probe"]
    assert report["rounds_completed"] >= 3


def test_run_pool_auto_advance_skips_dropped_case_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = tmp_path / "HSP-099-2026-05-01-dropped"
    cfg = _empty_config()
    cfg["decision"]["final_status"] = "drop"
    _write_gate(case, cfg)

    fake = _FakePoolRunner()
    monkeypatch.setattr(pool_advancer, "run_pool_parallel", fake)

    report = run_pool_auto_advance(
        [case],
        max_workers=1,
        run_pipeline_script=tmp_path / "fake.py",
        max_rounds=3,
    )

    assert fake.calls == []
    assert report["advanced_cases"] == 0
    assert len(report["stuck_cases"]) == 1
    assert "drop" in report["stuck_cases"][0]["stuck_at"]


def test_run_pool_auto_advance_include_publish_does_not_actually_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even with include_publish=True, the runner refuses to call pool_runner.

    The decision tree returns "publish", but the orchestrator catches it
    and short-circuits with a synthesized failed result -- pool publishing
    is forbidden by ``pool_runner.run_pool_parallel`` regardless.
    """
    case = tmp_path / "HSP-010-2026-05-01-ready"
    cfg = _config_after_data_probe_pass()
    cfg["execution_state"]["publish_readiness"]["status"] = "ready"
    cfg["execution_state"]["probe"]["last_run_at"] = "2026-05-01T01:00:00Z"
    _write_gate(case, cfg)

    fake = _FakePoolRunner()
    monkeypatch.setattr(pool_advancer, "run_pool_parallel", fake)

    report = run_pool_auto_advance(
        [case],
        max_workers=1,
        run_pipeline_script=tmp_path / "fake.py",
        max_rounds=1,
        include_publish=True,
    )

    # No real dispatch happened (pool runner forbids publish).
    assert all(call["mode"] != "publish" for call in fake.calls)
    # The single round logged a "publish" group with a synthesized refusal.
    publish_groups = [
        rr["groups"].get("publish") for rr in report["rounds"]
        if "publish" in rr["groups"]
    ]
    assert publish_groups, "expected synthesized publish group entry"


def test_run_pool_auto_advance_round_callback_receives_each_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = tmp_path / "HSP-001-2026-05-01-cb"
    _write_gate(case, _empty_config())
    fake = _FakePoolRunner()
    monkeypatch.setattr(pool_advancer, "run_pool_parallel", fake)

    seen: list[int] = []
    run_pool_auto_advance(
        [case],
        max_workers=1,
        run_pipeline_script=tmp_path / "fake.py",
        max_rounds=2,
        on_round_complete=lambda rr: seen.append(int(rr["round"])),
    )
    assert seen == [1, 2]


# --------------------------------------------------------------------------- #
# format_advance_summary
# --------------------------------------------------------------------------- #
def test_format_advance_summary_renders_rounds_and_stuck() -> None:
    report = {
        "rounds": [
            {"round": 1, "groups": {
                "discover": {"total": 2}, "probe": {"total": 1},
            }, "skipped": {}},
            {"round": 2, "groups": {}, "skipped": {}},
        ],
        "total_cases": 3,
        "advanced_cases": 3,
        "rounds_completed": 2,
        "stuck_cases": [{"case_dir": "/tmp/x", "stuck_at": "已 ready 等待人工 publish 决策"}],
    }
    text = format_advance_summary(report)
    assert "total_cases=3" in text
    assert "round 1" in text and "discover=2" in text
    assert "round 2" in text  # empty round still rendered
    assert "/tmp/x" in text
