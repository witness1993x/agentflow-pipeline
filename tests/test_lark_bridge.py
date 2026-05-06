from __future__ import annotations

from typing import Any

from agentflow_pipeline import lark_bridge


def test_bridge_descriptor_lists_git_case_commands() -> None:
    descriptor = lark_bridge.bridge_descriptor()

    assert descriptor["service"] == "agentflow-pipeline-lark-bridge"
    assert descriptor["endpoints"]["commands"] == "/api/git-case-commands"
    assert descriptor["endpoints"]["commands_compat"] == "/api/commands"
    commands = descriptor["commands"]
    assert "git_case_fork_rewrite" in commands
    assert "git_case_dry_publish" in commands
    assert all(name.startswith("git_case_") for name in commands)


def test_dispatch_bridge_command_calls_lark_callback(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_handle_event(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ack": True, "reply_card": {"ok": True}, "side_effects": ["case:fork-rewrite"]}

    monkeypatch.setattr(lark_bridge, "handle_event", fake_handle_event)

    result = lark_bridge.dispatch_bridge_command({
        "request_id": "req-1",
        "command": "git_case_fork_rewrite",
        "params": {
            "case_id": "HSP-005",
            "root": str(tmp_path),
            "operator_open_id": "ou_1",
        },
    })

    assert result["ok"] is True
    assert result["request_id"] == "req-1"
    assert result["command"] == "git_case_fork_rewrite"
    assert calls[0]["event_kind"] == "card_action"
    assert calls[0]["case_id"] == "HSP-005"
    assert calls[0]["action"] == "git_case_fork_rewrite"
    assert calls[0]["root"] == str(tmp_path)
    assert calls[0]["operator"]["open_id"] == "ou_1"


def test_dispatch_bridge_command_accepts_direct_lark_value_shape(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_handle_event(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ack": True, "reply_card": None, "side_effects": ["case:snooze"]}

    monkeypatch.setattr(lark_bridge, "handle_event", fake_handle_event)

    result = lark_bridge.dispatch_bridge_command({
        "value": {
            "action": "git_case_snooze",
            "case_id": "HSP-005",
            "days": "3d",
        },
        "operator": {"open_id": "ou_2"},
    })

    assert result["ok"] is True
    assert calls[0]["action"] == "git_case_snooze"
    assert calls[0]["payload"]["days"] == "3d"


def test_dispatch_bridge_command_rejects_non_git_command() -> None:
    result = lark_bridge.dispatch_bridge_command({
        "command": "lark_gate_b_approve",
        "params": {"case_id": "HSP-005"},
    })

    assert result["ok"] is False
    assert "unsupported command" in result["error"]
    assert "git_case_fork_rewrite" in result["supported_commands"]


def test_dispatch_bridge_command_requires_case_id() -> None:
    result = lark_bridge.dispatch_bridge_command({
        "command": "git_case_fork_rewrite",
        "params": {},
    })

    assert result["ok"] is False
    assert "case_id" in result["error"]
