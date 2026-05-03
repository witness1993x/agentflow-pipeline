"""Tests for build_command_inference module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentflow_pipeline.build_command_inference import (  # noqa: E402
    auto_fill_build_commands,
    build_commands_for_candidate,
    infer_build_commands_from_language,
    infer_build_commands_from_workspace,
    register_build_inference_args,
)


# ---------------------------------------------------------------------------
# Workspace-based detection
# ---------------------------------------------------------------------------


def test_package_json_with_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "scripts": {"build": "tsc", "test": "jest", "start": "node ."},
            }
        ),
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "npm ci || npm install"
    assert result["build"] == "npm run build"
    assert result["test"] == "npm test"
    assert result["language_detected"] == "javascript"
    assert result["confidence"] >= 70
    assert any("package.json" in line for line in result["evidence"])


def test_package_json_without_test_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"build": "tsc"}}),
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "npm ci || npm install"
    assert result["build"] == "npm run build"
    assert result["test"] == ""


def test_pnpm_lock_overrides_npm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 5.4\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert "pnpm" in result["install"]
    assert result["test"] == "pnpm test"
    assert any("pnpm-lock.yaml" in line for line in result["evidence"])


def test_yarn_lock_overrides_npm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"build": "rollup -c"}}),
        encoding="utf-8",
    )
    (tmp_path / "yarn.lock").write_text("# yarn lock\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert "yarn" in result["install"]
    assert result["build"] == "yarn build"


def test_pyproject_with_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[tool.pytest.ini_options]\nminversion = "6.0"\n',
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "pip install -e ."
    assert result["test"] == "pytest -q"
    assert result["language_detected"] == "python"


def test_pyproject_without_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "demo"\n',
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "pip install -e ."
    # pytest not detected => empty test
    assert result["test"] == ""


def test_pyproject_with_poetry(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "poetry install"


def test_requirements_txt_only(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "pip install -r requirements.txt"
    assert "pytest" in result["test"] or "unittest" in result["test"]
    assert result["language_detected"] == "python"


def test_cargo_toml_only(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == ""
    assert result["build"] == "cargo build --release"
    assert result["test"] == "cargo test"
    assert result["language_detected"] == "rust"


def test_go_mod_only(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.21\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "go mod download"
    assert result["build"] == "go build ./..."
    assert result["test"] == "go test ./..."


def test_makefile_with_targets(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "install:\n\tpip install -e .\n\nbuild:\n\techo build\n\ntest:\n\tpytest\n",
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "make install"
    assert result["build"] == "make build"
    assert result["test"] == "make test"


def test_makefile_partial_targets(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "build:\n\techo build\n\nlint:\n\techo lint\n",
        encoding="utf-8",
    )
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["build"] == "make build"
    assert result["install"] == ""
    assert result["test"] == ""


def test_priority_package_json_beats_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "jest"}}),
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM node:20\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["language_detected"] == "javascript"
    assert "docker build" not in result["build"]


def test_priority_pyproject_beats_requirements(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["install"] == "pip install -e ."


def test_dockerfile_only(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["build"] == "docker build -t local ."
    assert result["install"] == ""


def test_empty_workspace_returns_zero_confidence(tmp_path: Path) -> None:
    result = infer_build_commands_from_workspace(tmp_path)
    assert result["confidence"] == 0
    assert result["install"] == ""
    assert result["build"] == ""
    assert result["test"] == ""


# ---------------------------------------------------------------------------
# Language-based fallback
# ---------------------------------------------------------------------------


def test_language_fallback_typescript() -> None:
    result = infer_build_commands_from_language("TypeScript")
    assert "npm" in result["install"]
    assert result["build"] == "npm run build"
    assert result["test"] == "npm test"
    assert result["confidence"] <= 30


def test_language_fallback_python() -> None:
    result = infer_build_commands_from_language("Python")
    assert "pip" in result["install"]
    assert result["test"] == "pytest -q"


def test_language_fallback_rust() -> None:
    result = infer_build_commands_from_language("rust")
    assert result["build"] == "cargo build --release"


def test_language_fallback_go() -> None:
    result = infer_build_commands_from_language("Go")
    assert result["build"] == "go build ./..."


def test_language_fallback_java_rejected() -> None:
    result = infer_build_commands_from_language("Java")
    assert result["confidence"] == 0
    assert result["install"] == ""
    assert any("unknown_jvm_build_system" in e for e in result["evidence"])


def test_language_fallback_kotlin_rejected() -> None:
    result = infer_build_commands_from_language("Kotlin")
    assert result["confidence"] == 0


def test_language_fallback_unknown() -> None:
    result = infer_build_commands_from_language("brainfuck")
    assert result["confidence"] == 0
    assert any("unknown_language" in e for e in result["evidence"])


def test_language_fallback_empty() -> None:
    result = infer_build_commands_from_language("")
    assert result["confidence"] == 0


# ---------------------------------------------------------------------------
# build_commands_for_candidate combined entry
# ---------------------------------------------------------------------------


def test_combined_uses_workspace_when_populated(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    result = build_commands_for_candidate(
        tmp_path, {"language": "Python"}  # workspace must win
    )
    assert result["language_detected"] == "rust"


def test_combined_falls_back_to_language_when_empty(tmp_path: Path) -> None:
    result = build_commands_for_candidate(tmp_path, {"language": "TypeScript"})
    assert result["language_detected"] == "typescript"
    assert result["confidence"] <= 30


def test_combined_no_workspace_no_language() -> None:
    result = build_commands_for_candidate(None, {})
    assert result["confidence"] == 0


def test_combined_workspace_none_uses_language() -> None:
    result = build_commands_for_candidate(None, {"language": "Go"})
    assert result["build"] == "go build ./..."


# ---------------------------------------------------------------------------
# auto_fill_build_commands
# ---------------------------------------------------------------------------


def _make_config(install: str = "", build: str = "", test: str = "") -> dict:
    return {
        "gate_4_buildability": {
            "build_commands": {
                "install": install,
                "build": build,
                "test": test,
            }
        }
    }


def test_auto_fill_only_if_empty_preserves_existing(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    cfg = _make_config(build="my custom build", test="my custom test")
    summary = auto_fill_build_commands(
        cfg, {"language": "Rust"}, tmp_path, only_if_empty=True
    )
    cmds = cfg["gate_4_buildability"]["build_commands"]
    assert cmds["build"] == "my custom build"  # preserved
    assert cmds["test"] == "my custom test"  # preserved
    # install was empty, but Cargo.toml infers install="" too, so still empty
    assert cmds["install"] == ""
    assert "build" in summary["skipped"]
    assert "test" in summary["skipped"]


def test_auto_fill_only_if_empty_fills_blank_fields(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    cfg = _make_config(build="custom build")
    summary = auto_fill_build_commands(
        cfg, {"language": "Go"}, tmp_path, only_if_empty=True
    )
    cmds = cfg["gate_4_buildability"]["build_commands"]
    assert cmds["install"] == "go mod download"
    assert cmds["build"] == "custom build"  # preserved
    assert cmds["test"] == "go test ./..."
    assert summary["applied"].get("install") == "go mod download"
    assert summary["applied"].get("test") == "go test ./..."
    assert "build" in summary["skipped"]


def test_auto_fill_overwrite_replaces_existing(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    cfg = _make_config(install="old install", build="old build", test="old test")
    summary = auto_fill_build_commands(
        cfg, {"language": "Go"}, tmp_path, only_if_empty=False
    )
    cmds = cfg["gate_4_buildability"]["build_commands"]
    assert cmds["install"] == "go mod download"
    assert cmds["build"] == "go build ./..."
    assert cmds["test"] == "go test ./..."
    assert summary["applied"]["install"] == "go mod download"
    assert "skipped" in summary
    assert not summary["skipped"]  # nothing skipped under overwrite


def test_auto_fill_writes_inference_metadata(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    cfg: dict = {}
    auto_fill_build_commands(cfg, {"language": "Rust"}, tmp_path)
    inference = cfg["gate_4_buildability"]["inference"]
    assert inference["language_detected"] == "rust"
    assert inference["confidence"] >= 40
    assert isinstance(inference["evidence"], list)
    assert "last_inferred_at" in inference and inference["last_inferred_at"]


def test_auto_fill_no_workspace_uses_language(tmp_path: Path) -> None:
    cfg = _make_config()
    summary = auto_fill_build_commands(
        cfg, {"language": "Python"}, tmp_path, only_if_empty=True
    )
    cmds = cfg["gate_4_buildability"]["build_commands"]
    assert "pip" in cmds["install"]
    assert cmds["test"] == "pytest -q"
    assert summary["confidence"] <= 30


def test_auto_fill_inference_block_created_even_without_match(tmp_path: Path) -> None:
    cfg = _make_config()
    summary = auto_fill_build_commands(cfg, {}, tmp_path, only_if_empty=True)
    assert "inference" in cfg["gate_4_buildability"]
    assert summary["confidence"] == 0
    assert summary["applied"] == {}


# ---------------------------------------------------------------------------
# CLI argument registration
# ---------------------------------------------------------------------------


def test_register_build_inference_args() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    register_build_inference_args(parser)
    args = parser.parse_args([])
    assert args.auto_infer_build_commands is False
    assert args.auto_infer_confidence_threshold == 40
    assert args.auto_infer_overwrite is False

    args2 = parser.parse_args(
        [
            "--auto-infer-build-commands",
            "--auto-infer-confidence-threshold",
            "60",
            "--auto-infer-overwrite",
        ]
    )
    assert args2.auto_infer_build_commands is True
    assert args2.auto_infer_confidence_threshold == 60
    assert args2.auto_infer_overwrite is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
