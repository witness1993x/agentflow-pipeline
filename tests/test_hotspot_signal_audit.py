"""Tests for cli.audit_hotspot_signal_health.

Pins the placeholder-case rot warning: HSP-002-wallet-pnl-tracker sat in
``watch`` for 7 days with every signal dimension still 0. The audit kicks
in at inspect time so future placeholder cases get flagged.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agentflow_pipeline.cli import audit_hotspot_signal_health


def _base_config(*, final_status: str, dims: dict[str, int] | None = None) -> dict[str, Any]:
    if dims is None:
        dims = {
            "signal_strength": 0,
            "behavior_evidence": 0,
            "market_activity": 0,
            "timing_window": 0,
            "evidence_quality": 0,
        }
    return {
        "decision": {"final_status": final_status},
        "gate_1_hotspot_signal": {"dimensions": dict(dims)},
    }


def test_warns_when_watch_and_all_zero() -> None:
    cfg = _base_config(final_status="watch")
    warnings = audit_hotspot_signal_health(cfg)
    assert len(warnings) == 1
    assert "all zero" in warnings[0]
    assert "watch" in warnings[0]


def test_warns_when_probe_and_all_zero() -> None:
    cfg = _base_config(final_status="probe")
    warnings = audit_hotspot_signal_health(cfg)
    assert len(warnings) == 1
    assert "probe" in warnings[0]


def test_no_warning_when_any_dimension_nonzero() -> None:
    cfg = _base_config(
        final_status="watch",
        dims={
            "signal_strength": 3,
            "behavior_evidence": 0,
            "market_activity": 0,
            "timing_window": 0,
            "evidence_quality": 0,
        },
    )
    assert audit_hotspot_signal_health(cfg) == []


def test_no_warning_when_drop() -> None:
    """Drop is explicit retirement — warning is noise."""
    cfg = _base_config(final_status="drop")
    assert audit_hotspot_signal_health(cfg) == []


def test_no_warning_when_publish() -> None:
    """Already shipped — warning past ship is not actionable."""
    cfg = _base_config(final_status="publish")
    assert audit_hotspot_signal_health(cfg) == []


def test_no_warning_when_final_status_empty() -> None:
    """Freshly scaffolded; not yet promoted to watch."""
    cfg = _base_config(final_status="")
    assert audit_hotspot_signal_health(cfg) == []


def test_no_warning_when_dimensions_missing() -> None:
    cfg: dict[str, Any] = {
        "decision": {"final_status": "watch"},
        "gate_1_hotspot_signal": {},
    }
    assert audit_hotspot_signal_health(cfg) == []


def test_no_warning_when_dimensions_not_a_dict() -> None:
    cfg: dict[str, Any] = {
        "decision": {"final_status": "watch"},
        "gate_1_hotspot_signal": {"dimensions": "not-a-dict"},
    }
    assert audit_hotspot_signal_health(cfg) == []


def test_handles_string_dim_values_gracefully() -> None:
    cfg = _base_config(final_status="watch")
    cfg["gate_1_hotspot_signal"]["dimensions"]["signal_strength"] = "0"
    warnings = audit_hotspot_signal_health(cfg)
    assert len(warnings) == 1


def test_handles_garbage_dim_values_without_raising() -> None:
    cfg = _base_config(final_status="watch")
    cfg["gate_1_hotspot_signal"]["dimensions"]["signal_strength"] = "garbage"
    assert audit_hotspot_signal_health(cfg) == []


def test_does_not_mutate_input_config() -> None:
    cfg = _base_config(final_status="watch")
    snapshot = deepcopy(cfg)
    audit_hotspot_signal_health(cfg)
    assert cfg == snapshot
