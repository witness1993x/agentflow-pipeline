from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentflow_pipeline.lark_callback import handle_event


def _ready_config() -> dict[str, Any]:
    return {
        "meta": {
            "hotspot_id": "HSP-005",
            "hotspot_name": "demo-hotspot",
            "owner": "alice",
            "date": "2026-05-01",
        },
        "gate_2_project_shape": {"project_shape": "indexer"},
        "gate_3_repo_routing": {"repo_strategy": "fork_existing"},
        "gate_4_buildability": {"verdict": "pass", "kill_signals_triggered": []},
        "repo_plan": {
            "github_owner": "example-org",
            "repo_name": "demo-hotspot",
            "visibility": "public",
        },
        "decision": {
            "final_status": "publish_ready",
            "veto_from_gate": "",
            "next_review_date": "2026-05-08",
            "next_action": "ship it",
        },
        "pre_build_analysis": {
            "chainstream_fit": {"verdict": "pass", "score": 4},
        },
        "execution_state": {
            "publish_readiness": {"status": "ready"},
            "publish": {"publish_status": "not_started"},
        },
        "review_log": [],
    }


def _make_case(root: Path) -> Path:
    case_dir = root / "cases" / "HSP-005-demo-hotspot"
    case_dir.mkdir(parents=True, exist_ok=True)
    with (case_dir / "02-pipeline-gate.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(_ready_config(), fh, allow_unicode=True, sort_keys=False)
    return case_dir


def test_handle_event_dry_publish_returns_success_card(tmp_path: Path) -> None:
    _make_case(tmp_path)

    response = handle_event(
        event_kind="card_action",
        action="git_case_dry_publish",
        case_id="HSP-005",
        operator={"open_id": "ou_1", "name": "Op"},
        root=tmp_path,
    )

    assert response["ack"] is True
    assert response["side_effects"] == ["case:dry-publish"]
    assert response["reply_card"]["header"]["template"] == "green"
    body = response["reply_card"]["elements"][0]["text"]["content"]
    assert "**Case**: `HSP-005`" in body
    assert "`git_case_dry_publish`" in body
    assert "8 gates passed" in body


def test_handle_event_snooze_uses_payload_duration(tmp_path: Path) -> None:
    case_dir = _make_case(tmp_path)

    response = handle_event(
        event_kind="card_action",
        action="git_case_snooze",
        case_id="HSP-005",
        payload={"days": "3d"},
        operator={"open_id": "ou_2"},
        root=tmp_path,
    )

    assert response["reply_card"]["header"]["template"] == "green"
    cfg = yaml.safe_load((case_dir / "02-pipeline-gate.yaml").read_text())
    assert "snoozed for 3 days" in cfg["review_log"][-1]["what_changed"]


def test_handle_event_unknown_action_does_not_use_article_gate_vocab(tmp_path: Path) -> None:
    _make_case(tmp_path)

    response = handle_event(
        event_kind="card_action",
        action="lark_gate_b_approve",
        case_id="HSP-005",
        root=tmp_path,
    )

    assert response["ack"] is True
    assert response["reply_card"] is None
    assert response["side_effects"] == ["unknown_action"]
    assert "Unknown Git case action" in response["reply_text"]
