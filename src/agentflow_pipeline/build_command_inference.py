"""Build command inference for the run_pipeline probe stage.

This module fills in ``gate_4_buildability.build_commands.{install, build, test}``
by inspecting a candidate's checked-out workspace (preferred) or by falling
back to the candidate's declared GitHub language.

It is intentionally side-effect free against ``run_pipeline.py`` -- the caller
must opt in via the CLI flag ``--auto-infer-build-commands`` and wire the
result through ``auto_fill_build_commands`` before ``run_probe`` is invoked.

Integration patch (apply to ``run_pipeline.py``)
------------------------------------------------

1. At the top of ``run_pipeline.py``::

       from build_command_inference import (
           auto_fill_build_commands,
           register_build_inference_args,
       )

2. Inside ``parse_args`` (after the existing ``add_argument`` calls)::

       register_build_inference_args(parser)

3. Inside ``_run_probe_or_publish_branch``, after ``prepare_workspace`` and
   before ``run_probe``::

       if getattr(args, "auto_infer_build_commands", False):
           print_section("Build Command Inference")
           inference = auto_fill_build_commands(
               config,
               candidate,
               workspace,
               only_if_empty=not getattr(args, "auto_infer_overwrite", False),
           )
           threshold = int(getattr(args, "auto_infer_confidence_threshold", 40))
           if inference["confidence"] < threshold:
               print(
                   f"  inference confidence={inference['confidence']} below "
                   f"threshold={threshold}; build_commands left untouched"
               )
           else:
               for key, value in inference.get("applied", {}).items():
                   print(f"  applied {key}={value!r}")
               for key, value in inference.get("skipped", {}).items():
                   print(f"  skipped {key} (existing={value!r})")
               for line in inference.get("evidence", []):
                   print(f"  evidence: {line}")

The module also exposes a tiny ``__main__`` self-test that exercises the
common manifest paths without external dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Python 3.11+ ships tomllib; fall back to plain text scanning otherwise.
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    tomllib = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Public result shape helpers
# ---------------------------------------------------------------------------


def _empty_result() -> dict[str, Any]:
    return {
        "install": "",
        "build": "",
        "test": "",
        "language_detected": "",
        "evidence": [],
        "confidence": 0,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Manifest-specific detectors
# ---------------------------------------------------------------------------


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is not None:
        try:
            with path.open("rb") as fh:
                return tomllib.load(fh)
        except (OSError, ValueError):
            return {}
    # Fallback: best-effort plain text scan; we only ever look up presence of
    # a few well-known table headers.
    return {"__raw_text__": _read_text_safe(path)}


def _detect_node(workspace: Path) -> dict[str, Any] | None:
    pkg = workspace / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    scripts = scripts if isinstance(scripts, dict) else {}

    pkg_mgr = "npm"
    pm_evidence: list[str] = []
    if (workspace / "pnpm-lock.yaml").is_file():
        pkg_mgr = "pnpm"
        pm_evidence.append("detected: pnpm-lock.yaml")
    elif (workspace / "yarn.lock").is_file():
        pkg_mgr = "yarn"
        pm_evidence.append("detected: yarn.lock")
    elif (workspace / "package-lock.json").is_file():
        pm_evidence.append("detected: package-lock.json")

    install_cmds = {
        "npm": "npm ci || npm install",
        "pnpm": "pnpm install --frozen-lockfile || pnpm install",
        "yarn": "yarn install --frozen-lockfile || yarn install",
    }
    run_prefix = {"npm": "npm run", "pnpm": "pnpm", "yarn": "yarn"}[pkg_mgr]
    test_cmds = {"npm": "npm test", "pnpm": "pnpm test", "yarn": "yarn test"}

    install = install_cmds[pkg_mgr]
    build = ""
    test = ""
    evidence = ["detected: package.json", *pm_evidence]
    if "build" in scripts:
        build = f"{run_prefix} build"
        evidence.append("detected: package.json with scripts.build")
    if "test" in scripts:
        test = test_cmds[pkg_mgr]
        evidence.append("detected: package.json with scripts.test")

    confidence = 70
    if pm_evidence:
        confidence += 10
    if "build" in scripts:
        confidence += 5
    if "test" in scripts:
        confidence += 5
    confidence = min(confidence, 95)

    return {
        "install": install,
        "build": build,
        "test": test,
        "language_detected": "javascript",
        "evidence": evidence,
        "confidence": confidence,
    }


def _detect_pyproject(workspace: Path) -> dict[str, Any] | None:
    py = workspace / "pyproject.toml"
    if not py.is_file():
        return None
    data = _load_toml(py)
    raw = data.get("__raw_text__", "") if isinstance(data, dict) else ""

    uses_poetry = False
    has_pytest = False
    if raw:
        uses_poetry = "[tool.poetry]" in raw
        has_pytest = "[tool.pytest" in raw or "pytest" in raw
    else:
        tool = data.get("tool", {}) if isinstance(data, dict) else {}
        if isinstance(tool, dict):
            uses_poetry = "poetry" in tool
            has_pytest = "pytest" in tool
        # Also peek at dependencies for pytest.
        project = data.get("project", {}) if isinstance(data, dict) else {}
        if isinstance(project, dict):
            deps = project.get("dependencies", []) or []
            opt = project.get("optional-dependencies", {}) or {}
            blob = " ".join(str(x) for x in deps)
            if isinstance(opt, dict):
                for v in opt.values():
                    blob += " " + " ".join(str(x) for x in (v or []))
            if "pytest" in blob:
                has_pytest = True

    install = "poetry install" if uses_poetry else "pip install -e ."
    build = ""
    test = "pytest -q" if has_pytest else ""
    evidence = ["detected: pyproject.toml"]
    if uses_poetry:
        evidence.append("detected: pyproject.toml with [tool.poetry]")
    if has_pytest:
        evidence.append("detected: pyproject.toml with pytest config")

    confidence = 65
    if uses_poetry:
        confidence += 10
    if has_pytest:
        confidence += 5
    return {
        "install": install,
        "build": build,
        "test": test,
        "language_detected": "python",
        "evidence": evidence,
        "confidence": min(confidence, 90),
    }


def _detect_requirements(workspace: Path) -> dict[str, Any] | None:
    req = workspace / "requirements.txt"
    if not req.is_file():
        return None
    return {
        "install": "pip install -r requirements.txt",
        "build": "",
        "test": "pytest -q || python -m unittest",
        "language_detected": "python",
        "evidence": ["detected: requirements.txt"],
        "confidence": 60,
    }


def _detect_cargo(workspace: Path) -> dict[str, Any] | None:
    if not (workspace / "Cargo.toml").is_file():
        return None
    return {
        "install": "",
        "build": "cargo build --release",
        "test": "cargo test",
        "language_detected": "rust",
        "evidence": ["detected: Cargo.toml"],
        "confidence": 80,
    }


def _detect_go(workspace: Path) -> dict[str, Any] | None:
    if not (workspace / "go.mod").is_file():
        return None
    return {
        "install": "go mod download",
        "build": "go build ./...",
        "test": "go test ./...",
        "language_detected": "go",
        "evidence": ["detected: go.mod"],
        "confidence": 80,
    }


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:")


def _detect_makefile(workspace: Path) -> dict[str, Any] | None:
    mk = workspace / "Makefile"
    if not mk.is_file():
        return None
    text = _read_text_safe(mk)
    targets: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        m = _MAKE_TARGET.match(line)
        if m:
            targets.add(m.group(1))

    install = "make install" if "install" in targets else ""
    build = "make build" if "build" in targets else ""
    test = "make test" if "test" in targets else ""
    if not (install or build or test):
        return None

    evidence = ["detected: Makefile"]
    for tgt in ("install", "build", "test"):
        if tgt in targets:
            evidence.append(f"detected: Makefile target '{tgt}'")
    confidence = 50 + 5 * sum(1 for t in (install, build, test) if t)
    return {
        "install": install,
        "build": build,
        "test": test,
        "language_detected": "make",
        "evidence": evidence,
        "confidence": min(confidence, 75),
    }


def _detect_dockerfile(workspace: Path) -> dict[str, Any] | None:
    if not (workspace / "Dockerfile").is_file():
        return None
    return {
        "install": "",
        "build": "docker build -t local .",
        "test": "",
        "language_detected": "docker",
        "evidence": ["detected: Dockerfile"],
        "confidence": 35,
    }


# Order matters: highest priority first.
_DETECTORS = (
    _detect_node,
    _detect_pyproject,
    _detect_requirements,
    _detect_cargo,
    _detect_go,
    _detect_makefile,
    _detect_dockerfile,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_build_commands_from_workspace(workspace: Path) -> dict[str, Any]:
    """Scan ``workspace`` and return the highest-priority manifest match."""
    result = _empty_result()
    if not isinstance(workspace, Path):
        workspace = Path(workspace)
    if not workspace.exists() or not workspace.is_dir():
        return result

    for detector in _DETECTORS:
        match = detector(workspace)
        if match is not None:
            return match
    return result


_LANGUAGE_FALLBACKS: dict[str, dict[str, Any]] = {
    "javascript": {
        "install": "npm ci || npm install",
        "build": "",
        "test": "npm test",
        "language_detected": "javascript",
    },
    "typescript": {
        "install": "npm ci || npm install",
        "build": "npm run build",
        "test": "npm test",
        "language_detected": "typescript",
    },
    "python": {
        "install": "pip install -e . || pip install -r requirements.txt",
        "build": "",
        "test": "pytest -q",
        "language_detected": "python",
    },
    "rust": {
        "install": "",
        "build": "cargo build --release",
        "test": "cargo test",
        "language_detected": "rust",
    },
    "go": {
        "install": "go mod download",
        "build": "go build ./...",
        "test": "go test ./...",
        "language_detected": "go",
    },
}

_JVM_LANGUAGES = {"java", "kotlin", "scala", "groovy"}


def infer_build_commands_from_language(language: str) -> dict[str, Any]:
    """Fallback inference based on the candidate's declared language."""
    result = _empty_result()
    if not isinstance(language, str) or not language.strip():
        return result
    key = language.strip().lower()

    if key in _JVM_LANGUAGES:
        result["language_detected"] = key
        result["evidence"].append(f"rejected: unknown_jvm_build_system ({key})")
        result["confidence"] = 0
        return result

    base = _LANGUAGE_FALLBACKS.get(key)
    if base is None:
        result["evidence"].append(f"unknown_language: {language}")
        return result

    return {
        "install": base["install"],
        "build": base["build"],
        "test": base["test"],
        "language_detected": base["language_detected"],
        "evidence": [f"fallback: language={key}"],
        "confidence": 25,
    }


def _workspace_is_populated(workspace: Path | None) -> bool:
    if workspace is None:
        return False
    if not isinstance(workspace, Path):
        workspace = Path(workspace)
    if not workspace.exists() or not workspace.is_dir():
        return False
    try:
        next(workspace.iterdir())
    except StopIteration:
        return False
    except OSError:
        return False
    return True


def build_commands_for_candidate(
    workspace: Path | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Top-level entry: prefer workspace manifests, fall back to language."""
    if _workspace_is_populated(workspace):
        assert workspace is not None  # for type checkers
        ws_result = infer_build_commands_from_workspace(workspace)
        if ws_result["confidence"] > 0 or any(
            ws_result[k] for k in ("install", "build", "test")
        ):
            return ws_result
    language = ""
    if isinstance(candidate, dict):
        language = str(candidate.get("language") or "")
    return infer_build_commands_from_language(language)


def auto_fill_build_commands(
    config: dict[str, Any],
    candidate: dict[str, Any],
    workspace: Path | None,
    *,
    only_if_empty: bool = True,
) -> dict[str, Any]:
    """Write the inferred commands into ``config`` and return a summary dict."""
    inferred = build_commands_for_candidate(workspace, candidate)

    gate4 = config.setdefault("gate_4_buildability", {})
    if not isinstance(gate4, dict):
        gate4 = {}
        config["gate_4_buildability"] = gate4
    commands = gate4.setdefault("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
        gate4["build_commands"] = commands

    applied: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for key in ("install", "build", "test"):
        new_value = inferred.get(key, "")
        existing = commands.get(key, "")
        existing_str = str(existing) if existing is not None else ""
        if not new_value:
            # Nothing to apply for this slot.
            if existing_str:
                skipped[key] = existing_str
            continue
        if only_if_empty and existing_str.strip():
            skipped[key] = existing_str
            continue
        commands[key] = new_value
        applied[key] = new_value

    gate4["inference"] = {
        "language_detected": inferred.get("language_detected", ""),
        "evidence": list(inferred.get("evidence", [])),
        "confidence": int(inferred.get("confidence", 0)),
        "last_inferred_at": _utc_now(),
    }

    return {
        "applied": applied,
        "skipped": skipped,
        "evidence": list(inferred.get("evidence", [])),
        "confidence": int(inferred.get("confidence", 0)),
    }


def register_build_inference_args(parser: argparse.ArgumentParser) -> None:
    """Register the auto-inference CLI flags on an existing parser."""
    parser.add_argument(
        "--auto-infer-build-commands",
        action="store_true",
        default=False,
        help=(
            "Auto-detect install/build/test commands from the candidate "
            "workspace (or language) before running the probe."
        ),
    )
    parser.add_argument(
        "--auto-infer-confidence-threshold",
        type=int,
        default=40,
        help=(
            "Minimum inference confidence (0-100) required to write commands. "
            "Default: 40."
        ),
    )
    parser.add_argument(
        "--auto-infer-overwrite",
        action="store_true",
        default=False,
        help=(
            "Overwrite already-populated build_commands fields. By default, "
            "only empty fields are filled."
        ),
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        node_dir = root / "node_app"
        node_dir.mkdir()
        (node_dir / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"build": "tsc", "test": "jest"}}),
            encoding="utf-8",
        )
        node_result = infer_build_commands_from_workspace(node_dir)
        assert node_result["install"] == "npm ci || npm install", node_result
        assert node_result["build"] == "npm run build"
        assert node_result["test"] == "npm test"

        py_dir = root / "py_app"
        py_dir.mkdir()
        (py_dir / "requirements.txt").write_text("requests\n", encoding="utf-8")
        py_result = infer_build_commands_from_workspace(py_dir)
        assert py_result["install"] == "pip install -r requirements.txt", py_result

        rust_dir = root / "rust_app"
        rust_dir.mkdir()
        (rust_dir / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        rust_result = infer_build_commands_from_workspace(rust_dir)
        assert rust_result["build"] == "cargo build --release", rust_result

        cfg: dict[str, Any] = {}
        summary = auto_fill_build_commands(cfg, {"language": "Rust"}, rust_dir)
        assert summary["applied"]["build"] == "cargo build --release"
        assert cfg["gate_4_buildability"]["build_commands"]["test"] == "cargo test"

        print("build_command_inference self-test OK")
