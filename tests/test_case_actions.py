"""Tests for ``agentflow_pipeline.case_actions``.

Zero network: every test uses ``tmp_path`` + a fake case YAML; the Anthropic
SDK is monkeypatched (or its env var stripped) so :func:`handle_write_stub`
exercises only the static-skeleton fallback path.
"""
from __future__ import annotations

import sys
import types
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from agentflow_pipeline import case_actions
from agentflow_pipeline.case_actions import (
    dispatch_callback_action,
    handle_drop,
    handle_dry_publish,
    handle_fork_rewrite,
    handle_snooze,
    handle_write_stub,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ready_config() -> Dict[str, Any]:
    """Mirror the conftest ready_config but inline so this test is self-contained."""
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


def _blocked_config() -> Dict[str, Any]:
    cfg = _ready_config()
    cfg["execution_state"]["publish_readiness"]["status"] = "blocked_buildability"
    cfg["gate_4_buildability"]["kill_signals_triggered"] = ["build_timeout"]
    cfg["pre_build_analysis"]["chainstream_fit"]["verdict"] = "hold"
    cfg["gate_3_repo_routing"]["repo_strategy"] = "undecided"
    cfg["repo_plan"]["github_owner"] = ""
    cfg["decision"]["veto_from_gate"] = "gate_4_buildability"
    return cfg


def _make_case(root: Path, case_id: str, slug_suffix: str, config: Dict[str, Any]) -> Path:
    """Materialise ``<root>/cases/<case_id>-<slug>/02-pipeline-gate.yaml``."""
    case_dir = root / "cases" / f"{case_id}-{slug_suffix}"
    case_dir.mkdir(parents=True, exist_ok=True)
    gate = case_dir / "02-pipeline-gate.yaml"
    with gate.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)
    return case_dir


@pytest.fixture
def root_with_ready_case(tmp_path: Path) -> Path:
    _make_case(tmp_path, "HSP-005", "demo-hotspot", _ready_config())
    return tmp_path


@pytest.fixture
def root_with_blocked_case(tmp_path: Path) -> Path:
    _make_case(tmp_path, "HSP-005", "blocked-hotspot", _blocked_config())
    return tmp_path


@pytest.fixture(autouse=True)
def _strip_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: tests never see ANTHROPIC_API_KEY unless explicitly set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# dispatch_callback_action
# ---------------------------------------------------------------------------

def test_dispatch_dry_publish_routes_to_handler(root_with_ready_case: Path) -> None:
    result = dispatch_callback_action(
        "case:dry-publish:HSP-005",
        root=root_with_ready_case,
        actor="tg-user",
    )
    assert result["action"] == "case:dry-publish"
    assert result["case_id"] == "HSP-005"
    assert result["success"] is True
    assert "8 gates passed" in result["summary"]


def test_dispatch_snooze_parses_7d_extra(root_with_ready_case: Path) -> None:
    result = dispatch_callback_action(
        "case:snooze:HSP-005:7d",
        root=root_with_ready_case,
    )
    assert result["action"] == "case:snooze"
    assert result["success"] is True
    assert "snoozed for 7 days" in result["summary"]


def test_dispatch_fork_rewrite_routes_to_handler(root_with_ready_case: Path) -> None:
    result = dispatch_callback_action(
        "case:fork-rewrite:HSP-005",
        root=root_with_ready_case,
    )
    assert result["action"] == "case:fork-rewrite"
    assert result["success"] is True
    assert "ChainStream rewrite ready" in result["summary"]


def test_dispatch_unknown_verb_fails(root_with_ready_case: Path) -> None:
    result = dispatch_callback_action(
        "case:teleport:HSP-005",
        root=root_with_ready_case,
    )
    assert result["success"] is False
    assert "Unknown action" in result["summary"]


def test_dispatch_invalid_case_id_format(root_with_ready_case: Path) -> None:
    result = dispatch_callback_action(
        "case:drop:HSP_005",
        root=root_with_ready_case,
    )
    assert result["success"] is False
    assert "invalid case_id" in result["summary"].lower()


def test_dispatch_case_id_not_found(tmp_path: Path) -> None:
    (tmp_path / "cases").mkdir()
    result = dispatch_callback_action(
        "case:drop:HSP-999",
        root=tmp_path,
    )
    assert result["success"] is False
    assert "not found" in result["summary"]
    assert any("hint" == fu.get("kind") for fu in result["follow_up"])


def test_dispatch_malformed_callback_data_too_few_parts(tmp_path: Path) -> None:
    result = dispatch_callback_action("case:drop", root=tmp_path)
    assert result["success"] is False
    assert "malformed callback_data" in result["summary"]


def test_dispatch_handler_crash_is_caught(
    monkeypatch: pytest.MonkeyPatch,
    root_with_ready_case: Path,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(case_actions._HANDLERS, "drop", boom)
    result = dispatch_callback_action(
        "case:drop:HSP-005",
        root=root_with_ready_case,
    )
    assert result["success"] is False
    assert "Action handler crashed" in result["summary"]
    assert "kaboom" in result["summary"]


# ---------------------------------------------------------------------------
# handle_dry_publish
# ---------------------------------------------------------------------------

def test_handle_dry_publish_ready_config(root_with_ready_case: Path) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_dry_publish(case_dir, actor="tg-user")
    assert result["success"] is True
    assert "8 gates passed" in result["summary"]
    # follow_up always includes the auto-publish hint
    assert any("--auto-publish" in fu.get("text", "") for fu in result["follow_up"])


def test_handle_dry_publish_blocked_lists_first_three_blockers(
    root_with_blocked_case: Path,
) -> None:
    case_dir = root_with_blocked_case / "cases" / "HSP-005-blocked-hotspot"
    result = handle_dry_publish(case_dir, actor="tg-user")
    assert result["success"] is False
    # summary mentions blocker count
    assert "blockers" in result["summary"].lower()
    # full blocker list lives in follow_up
    blockers_entry = next(
        (fu for fu in result["follow_up"] if fu.get("kind") == "blockers"), None
    )
    assert blockers_entry is not None
    assert blockers_entry["count"] >= 3
    assert len(blockers_entry["items"]) == blockers_entry["count"]


# ---------------------------------------------------------------------------
# handle_fork_rewrite
# ---------------------------------------------------------------------------

def test_handle_fork_rewrite_creates_chainstream_workspace(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_fork_rewrite(case_dir, actor="lark:ou_1", root=root_with_ready_case)

    assert result["success"] is True
    workspace = root_with_ready_case / "workspaces" / "HSP-005-demo-hotspot"
    assert (workspace / "src" / "chainstream-client.ts").is_file()
    assert (workspace / "src" / "chainstream-probe.ts").is_file()
    assert (workspace / ".env.chainstream.example").is_file()
    assert (workspace / "CHAINSTREAM_REWRITE.md").is_file()
    assert "CHAINSTREAM_API_KEY" in (
        workspace / ".env.chainstream.example"
    ).read_text(encoding="utf-8")
    assert any(
        fu.get("kind") == "chainstream_rewrite" for fu in result["follow_up"]
    )


def test_handle_fork_rewrite_records_gate_state(root_with_ready_case: Path) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    handle_fork_rewrite(case_dir, actor="lark:ou_1", root=root_with_ready_case)

    cfg = yaml.safe_load((case_dir / "02-pipeline-gate.yaml").read_text())
    state = cfg["execution_state"]["chainstream_rewrite"]
    assert state["status"] == "rewritten"
    assert state["actor"] == "lark:ou_1"
    assert "ChainStream rewrite applied" in cfg["review_log"][-1]["what_changed"]


# ---------------------------------------------------------------------------
# handle_drop
# ---------------------------------------------------------------------------

def test_handle_drop_writes_final_status_drop_and_appends_log(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    before_log_len = len(_ready_config()["review_log"])

    result = handle_drop(case_dir, actor="tg-user")
    assert result["success"] is True
    assert "HSP-005" in result["summary"]

    with (case_dir / "02-pipeline-gate.yaml").open("r", encoding="utf-8") as fh:
        config_after = yaml.safe_load(fh)
    assert config_after["decision"]["final_status"] == "drop"
    assert len(config_after["review_log"]) == before_log_len + 1
    assert config_after["review_log"][-1]["new_status"] == "drop"


def test_handle_drop_actor_recorded_in_next_action(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    handle_drop(case_dir, actor="alice@example.com")
    with (case_dir / "02-pipeline-gate.yaml").open("r", encoding="utf-8") as fh:
        config_after = yaml.safe_load(fh)
    assert "alice@example.com" in config_after["decision"]["next_action"]
    # also stamped with an iso timestamp; sanity-check it's iso-ish.
    assert "T" in config_after["decision"]["next_action"]


# ---------------------------------------------------------------------------
# handle_snooze
# ---------------------------------------------------------------------------

def test_handle_snooze_pushes_next_review_date_seven_days(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_snooze(case_dir, actor="tg-user", days="7d")
    assert result["success"] is True

    with (case_dir / "02-pipeline-gate.yaml").open("r", encoding="utf-8") as fh:
        config_after = yaml.safe_load(fh)

    expected = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    assert config_after["decision"]["next_review_date"] == expected
    assert config_after["review_log"][-1]["what_changed"].startswith(
        "snoozed for 7 days"
    )


def test_handle_snooze_invalid_format_fails(root_with_ready_case: Path) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_snooze(case_dir, actor="tg-user", days="banana")
    assert result["success"] is False
    assert "invalid snooze duration" in result["summary"]


def test_handle_snooze_out_of_range_fails(root_with_ready_case: Path) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_snooze(case_dir, actor="tg-user", days="99d")
    assert result["success"] is False
    assert "out of range" in result["summary"]


# ---------------------------------------------------------------------------
# handle_write_stub
# ---------------------------------------------------------------------------

def test_handle_write_stub_no_api_key_falls_back_to_static(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_write_stub(
        case_dir,
        actor="tg-user",
        root=root_with_ready_case,
    )
    assert result["success"] is True
    assert "static skeleton" in result["summary"]
    assert "ANTHROPIC_API_KEY" in result["summary"]


def test_handle_write_stub_creates_minimal_files(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_write_stub(
        case_dir,
        actor="tg-user",
        root=root_with_ready_case,
    )
    assert result["success"] is True

    workspace = root_with_ready_case / "workspaces" / "HSP-005-demo-hotspot"
    assert (workspace / "package.json").is_file()
    assert (workspace / "tsconfig.json").is_file()
    assert (workspace / "src" / "index.ts").is_file()
    assert (workspace / "README.md").is_file()
    # follow-up includes the npm install hint
    assert any("npm install" in fu.get("text", "") for fu in result["follow_up"])


def test_handle_write_stub_does_not_overwrite_existing_files(
    root_with_ready_case: Path,
) -> None:
    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    workspace = root_with_ready_case / "workspaces" / "HSP-005-demo-hotspot"
    workspace.mkdir(parents=True, exist_ok=True)
    custom = "// MY CUSTOM CODE — DO NOT TOUCH\n"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "index.ts").write_text(custom, encoding="utf-8")

    result = handle_write_stub(
        case_dir,
        actor="tg-user",
        root=root_with_ready_case,
    )
    assert result["success"] is True
    # File preserved verbatim
    assert (workspace / "src" / "index.ts").read_text(encoding="utf-8") == custom

    stub_summary = next(
        (fu for fu in result["follow_up"] if fu.get("kind") == "stub_summary"),
        None,
    )
    assert stub_summary is not None
    assert "src/index.ts" in stub_summary["skipped"]


def test_handle_write_stub_uses_claude_when_sdk_available(
    monkeypatch: pytest.MonkeyPatch,
    root_with_ready_case: Path,
) -> None:
    """When ANTHROPIC_API_KEY is set and the SDK works, stub uses Claude output."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    fake_body = (
        "===FILE: package.json===\n"
        '{"name":"@hotspot/from-claude","version":"0.0.1"}\n'
        "===END===\n"
        "===FILE: src/index.ts===\n"
        "export const FROM_CLAUDE = true;\n"
        "===END===\n"
    )

    class _FakeBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeResponse:
        def __init__(self) -> None:
            self.content = [_FakeBlock(fake_body)]

    class _FakeMessages:
        def create(self, **_: Any) -> _FakeResponse:
            return _FakeResponse()

    class _FakeAnthropic:
        def __init__(self, **_: Any) -> None:
            self.messages = _FakeMessages()

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_write_stub(
        case_dir,
        actor="tg-user",
        root=root_with_ready_case,
    )
    assert result["success"] is True
    assert "review then ship" in result["summary"]

    workspace = root_with_ready_case / "workspaces" / "HSP-005-demo-hotspot"
    pkg = (workspace / "package.json").read_text(encoding="utf-8")
    assert "from-claude" in pkg
    assert (workspace / "src" / "index.ts").read_text(encoding="utf-8").startswith(
        "export const FROM_CLAUDE"
    )


def test_handle_write_stub_claude_failure_falls_back_silently(
    monkeypatch: pytest.MonkeyPatch,
    root_with_ready_case: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    class _FakeMessages:
        def create(self, **_: Any) -> Any:
            raise RuntimeError("simulated network error")

    class _FakeAnthropic:
        def __init__(self, **_: Any) -> None:
            self.messages = _FakeMessages()

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    case_dir = root_with_ready_case / "cases" / "HSP-005-demo-hotspot"
    result = handle_write_stub(
        case_dir,
        actor="tg-user",
        root=root_with_ready_case,
    )
    assert result["success"] is True
    # Static fallback summary differs from the no-key case.
    assert "static skeleton" in result["summary"]
    workspace = root_with_ready_case / "workspaces" / "HSP-005-demo-hotspot"
    assert (workspace / "package.json").is_file()
