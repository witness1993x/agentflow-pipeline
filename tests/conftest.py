"""Shared pytest fixtures and sys.path setup for the test suite.

Adds ``src/`` to ``sys.path`` so tests can do
``from agentflow_pipeline.auto_publish import ...`` etc, without requiring an
editable install. The repo root is also added so the legacy shim
``run_pipeline.py`` remains importable for any tests that still use it.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict

import pytest


# ---------------------------------------------------------------------------
# sys.path: src/ first (namespace), then repo root (legacy shim)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
for _p in (_SRC_ROOT, _REPO_ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Common config fixtures
# ---------------------------------------------------------------------------

def _make_ready_config() -> Dict[str, Any]:
    """Return a fully-populated 'ready' gate config used across tests."""
    return {
        "meta": {
            "hotspot_id": "HSP-001",
            "hotspot_name": "demo-hotspot",
            "owner": "alice",
            "date": "2026-05-01",
        },
        "source_context": {"topic_lineage": ["solana", "dex"]},
        "gate_2_project_shape": {"project_shape": "indexer"},
        "gate_3_repo_routing": {
            "repo_strategy": "fork_existing",
            "discovered_query": "",
            "candidate_repos": [],
            "recommended_reason": "fit + activity",
        },
        "gate_4_buildability": {
            "verdict": "pass",
            "score": 4,
            "kill_signals": [],
            "kill_signals_triggered": [],
            "build_commands": {
                "install": "pip install -e .",
                "build": "python -m build",
                "test": "pytest -q",
            },
        },
        "gate_5_publish_decision": {"verdict": "pass", "score": 4},
        "repo_plan": {
            "github_owner": "example-org",
            "repo_name": "demo-hotspot",
            "visibility": "public",
            "default_branch": "main",
        },
        "repo_meta": {"default_branch": "main", "language": "Python"},
        "decision": {
            "final_status": "publish_ready",
            "veto_from_gate": "",
            "next_review_date": "2026-05-08",
            "one_line_thesis": "demo thesis",
        },
        "pre_build_analysis": {
            "chainstream_fit": {
                "verdict": "pass",
                "target_capability": "graphql",
                "score": 4,
            },
        },
        "execution_state": {
            "publish_readiness": {"status": "ready"},
            "publish": {"publish_status": "not_started"},
            "data_probe": {"status": "passed"},
            "probe": {
                "install_status": "passed",
                "build_status": "passed",
                "test_status": "passed",
            },
            "kafka_probe": {"status": "passed"},
        },
    }


def _make_blocked_config() -> Dict[str, Any]:
    """Return a config with buildability blocked."""
    cfg = _make_ready_config()
    cfg["execution_state"]["publish_readiness"]["status"] = "blocked_buildability"
    cfg["execution_state"]["probe"]["build_status"] = "failed"
    cfg["execution_state"]["probe"]["test_status"] = "failed"
    cfg["gate_4_buildability"]["verdict"] = "fail"
    cfg["gate_4_buildability"]["kill_signals_triggered"] = ["build_timeout"]
    cfg["pre_build_analysis"]["chainstream_fit"]["verdict"] = "hold"
    cfg["gate_3_repo_routing"]["repo_strategy"] = "undecided"
    cfg["repo_plan"]["github_owner"] = ""
    cfg["decision"]["veto_from_gate"] = "gate_4_buildability"
    return cfg


@pytest.fixture
def ready_config() -> Dict[str, Any]:
    """Provide a fresh deep copy of a ready-state gate config."""
    return deepcopy(_make_ready_config())


@pytest.fixture
def blocked_config() -> Dict[str, Any]:
    """Provide a fresh deep copy of a buildability-blocked gate config."""
    return deepcopy(_make_blocked_config())


# ---------------------------------------------------------------------------
# Fake subprocess.CompletedProcess factory
# ---------------------------------------------------------------------------

class FakeCompleted:
    """Stand-in for ``subprocess.CompletedProcess`` with the fields we use."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FakeCompleted(rc={self.returncode}, out={self.stdout!r})"


def _fake_run_command_factory(specs: Dict[str, Any]) -> Callable[..., FakeCompleted]:
    """Build a callable mimicking ``run_pipeline.run_command``.

    ``specs`` keys are matched as substrings against the ``" ".join(cmd)``
    rendering of the invoked command. The first matching spec wins.

    Each value may be:
      * a ``FakeCompleted`` instance (returned verbatim),
      * a ``dict`` with optional ``returncode`` / ``stdout`` / ``stderr`` keys,
      * an ``Exception`` instance (raised),
      * a callable ``(cmd, cwd) -> FakeCompleted`` (called).

    Unmatched commands return ``FakeCompleted(returncode=1, stderr="no match")``.
    """
    def _runner(cmd, cwd=None):  # type: ignore[no-untyped-def]
        joined = " ".join(str(p) for p in cmd)
        for needle, spec in specs.items():
            if needle in joined:
                if isinstance(spec, FakeCompleted):
                    return spec
                if isinstance(spec, Exception):
                    raise spec
                if callable(spec):
                    return spec(cmd, cwd)
                if isinstance(spec, dict):
                    return FakeCompleted(
                        returncode=int(spec.get("returncode", 0)),
                        stdout=str(spec.get("stdout", "")),
                        stderr=str(spec.get("stderr", "")),
                    )
        return FakeCompleted(returncode=1, stderr="no match")

    return _runner


@pytest.fixture
def fake_run_command():
    """Factory fixture for building stub ``run_command`` callables."""
    return _fake_run_command_factory
