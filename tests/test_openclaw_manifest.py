from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATHS = [
    ROOT / "openclaw.plugin.json",
    ROOT / "skill" / "openclaw.plugin.json",
    ROOT / "skill" / "bundle" / "openclaw.plugin.json",
]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_openclaw_manifest_version_matches_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]

    assert version == "0.4.7"
    for path in MANIFEST_PATHS:
        assert _load_json(path)["version"] == version


def test_openclaw_manifest_does_not_claim_feishu_channel() -> None:
    for path in MANIFEST_PATHS:
        manifest = _load_json(path)
        assert manifest.get("channels") == []
        assert manifest.get("channelConfigs") == {}


def test_openclaw_manifest_documents_official_lark_companion() -> None:
    for path in MANIFEST_PATHS:
        manifest = _load_json(path)
        props = manifest["configSchema"]["properties"]
        assert props["lark_integration_mode"]["default"] == "standalone_webhook"
        assert "openclaw_lark_channel" in props["lark_integration_mode"]["enum"]
        assert props["openclaw_lark_plugin"]["default"] == "@larksuite/openclaw-lark"


def test_openclaw_manifest_exposes_lark_card_and_callback_handlers() -> None:
    for path in MANIFEST_PATHS:
        manifest = _load_json(path)
        entry_points = manifest["entryPoints"]
        assert (
            entry_points["lark_scan_card"]["handler"]
            == "agentflow_pipeline.lark_callback:build_scan_interactive_card"
        )
        assert (
            entry_points["lark_callback"]["handler"]
            == "agentflow_pipeline.lark_callback:handle_event"
        )
