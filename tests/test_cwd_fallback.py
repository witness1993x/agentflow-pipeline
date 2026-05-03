"""Self-correction of ROOT when the user invokes the CLI from an off-root cwd.

Background
----------
A repeat real-world failure: the user prepares source code under
``<framework>/workspaces/HSP-XXX/``, ``cd``s into that workspace to run
``npm install``, then invokes::

    agentflow-pipeline --case-dir /abs/path/to/<framework>/cases/HSP-XXX ...

without ``cd``-ing back to the framework root. Because ROOT defaults to
``Path.cwd()``, ``DEFAULT_WORKSPACE_ROOT`` becomes ``<workspace>/workspaces``
and ``workspace_dir`` ends up doubly-nested (``<workspace>/workspaces/HSP-XXX-.../HSP-XXX-...``).
The framework prepares/probes/publishes the wrong (empty) tree and the
user's real source code never makes it onto GitHub.

This module exercises the ``_auto_correct_root_from_case_dir`` self-correct
helper plus a single end-to-end subprocess sanity check.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

# Make the unit-test imports resolve regardless of how pytest was invoked.
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agentflow_pipeline.cli import _auto_correct_root_from_case_dir  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_fake_framework(tmp_path: Path, hsp_id: str = "HSP-001") -> tuple[Path, Path]:
    """Build a minimal framework layout under tmp_path.

    Returns:
        (framework_root, case_dir) -- e.g. (/tmp/.../fw, /tmp/.../fw/cases/HSP-001-2026-05-01-foo)
    """
    framework = tmp_path / "fake_fw"
    case_dir = framework / "cases" / f"{hsp_id}-2026-05-01-foo"
    case_dir.mkdir(parents=True, exist_ok=True)
    # A minimal gate file so callers that try to load it succeed.
    gate_file = case_dir / "02-pipeline-gate.yaml"
    gate_file.write_text(
        f"meta:\n  hotspot_id: {hsp_id}\n  hotspot_name: foo\n  date: 2026-05-01\n",
        encoding="utf-8",
    )
    return framework, case_dir


def _ns(**overrides) -> argparse.Namespace:
    """Build an argparse Namespace prefilled with defaults the helper inspects."""
    defaults = dict(
        root="",
        case_dir=None,
        gate_file=None,
        mode="inspect",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _env_with_src(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env.pop("AGENTFLOW_ROOT", None)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{SRC_ROOT}{os.pathsep}{existing}" if existing else str(SRC_ROOT)
    )
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# Unit tests for _auto_correct_root_from_case_dir
# --------------------------------------------------------------------------- #
def test_self_correct_triggers_when_cwd_unrelated(tmp_path, monkeypatch, capsys):
    """cwd is a completely unrelated dir; --case-dir points into a real
    framework. Helper should return the inferred framework root."""
    framework, case_dir = _make_fake_framework(tmp_path)
    foreign = tmp_path / "some_other_dir"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    args = _ns(case_dir=str(case_dir))
    new_root = _auto_correct_root_from_case_dir(args, foreign.resolve())

    assert new_root == framework.resolve()
    err = capsys.readouterr().err
    assert "auto-corrected ROOT" in err
    assert str(framework.resolve()) in err


def test_self_correct_triggers_when_cwd_inside_nested_workspace(tmp_path, monkeypatch, capsys):
    """cwd is inside ``<framework>/workspaces/HSP-X/...``; helper must escape
    the workspace and infer the framework root from --case-dir."""
    framework, case_dir = _make_fake_framework(tmp_path)
    nested_ws = framework / "workspaces" / "HSP-001-2026-05-01-foo"
    nested_ws.mkdir(parents=True)
    monkeypatch.chdir(nested_ws)

    args = _ns(case_dir=str(case_dir))
    new_root = _auto_correct_root_from_case_dir(args, nested_ws.resolve())

    assert new_root == framework.resolve()
    # The doubly-nested workspace path that triggered the original bug must
    # never appear once we're using the self-corrected root.
    derived_ws = new_root / "workspaces"
    assert derived_ws == framework / "workspaces"
    assert "auto-corrected ROOT" in capsys.readouterr().err


def test_explicit_root_flag_disables_self_correct(tmp_path, monkeypatch, capsys):
    """When the user passes --root explicitly we always honour it."""
    framework, case_dir = _make_fake_framework(tmp_path)
    elsewhere = tmp_path / "explicit_root"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)  # cwd unrelated

    args = _ns(case_dir=str(case_dir), root=str(elsewhere))
    new_root = _auto_correct_root_from_case_dir(args, elsewhere.resolve())

    assert new_root == elsewhere.resolve()
    assert "auto-corrected" not in capsys.readouterr().err


def test_agentflow_root_env_disables_self_correct(tmp_path, monkeypatch, capsys):
    """When AGENTFLOW_ROOT env var is set, never silently override it."""
    framework, case_dir = _make_fake_framework(tmp_path)
    env_root = tmp_path / "env_root"
    env_root.mkdir()
    monkeypatch.setenv("AGENTFLOW_ROOT", str(env_root))
    monkeypatch.chdir(tmp_path)

    args = _ns(case_dir=str(case_dir))
    new_root = _auto_correct_root_from_case_dir(args, env_root.resolve())

    assert new_root == env_root.resolve()
    assert "auto-corrected" not in capsys.readouterr().err


def test_gate_file_path_also_triggers_self_correct(tmp_path, monkeypatch, capsys):
    """``--gate-file <framework>/cases/HSP-X/02-pipeline-gate.yaml`` must
    self-correct the same way ``--case-dir`` does."""
    framework, case_dir = _make_fake_framework(tmp_path)
    gate_file = case_dir / "02-pipeline-gate.yaml"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    args = _ns(gate_file=str(gate_file))
    new_root = _auto_correct_root_from_case_dir(args, foreign.resolve())

    assert new_root == framework.resolve()
    assert "auto-corrected ROOT" in capsys.readouterr().err


def test_pool_mode_skips_self_correct(tmp_path, monkeypatch, capsys):
    """``--mode pool`` has no case-dir; helper must short-circuit."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    args = _ns(mode="pool")
    new_root = _auto_correct_root_from_case_dir(args, foreign.resolve())

    assert new_root == foreign.resolve()
    assert "auto-corrected" not in capsys.readouterr().err


def test_non_cases_layout_skips_self_correct(tmp_path, monkeypatch, capsys):
    """Backward-compat: if the user keeps cases under a non-standard parent
    dir (e.g. ``my-cases/`` instead of ``cases/``), don't touch their ROOT."""
    weird_root = tmp_path / "weird_fw"
    weird_case = weird_root / "my-cases" / "HSP-001-foo"  # parent is "my-cases"
    weird_case.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    args = _ns(case_dir=str(weird_case))
    new_root = _auto_correct_root_from_case_dir(args, foreign.resolve())

    # No correction: parent is not "cases".
    assert new_root == foreign.resolve()
    assert "auto-corrected" not in capsys.readouterr().err


def test_no_correction_when_cwd_already_matches_framework(tmp_path, monkeypatch, capsys):
    """When cwd is already the framework root, no warning or change."""
    framework, case_dir = _make_fake_framework(tmp_path)
    monkeypatch.chdir(framework)

    args = _ns(case_dir=str(case_dir))
    new_root = _auto_correct_root_from_case_dir(args, framework.resolve())

    assert new_root == framework.resolve()
    assert "auto-corrected" not in capsys.readouterr().err


def test_no_case_or_gate_dir_skips(tmp_path, monkeypatch):
    """Args with neither --case-dir nor --gate-file must short-circuit cleanly."""
    monkeypatch.chdir(tmp_path)
    args = _ns()  # both case_dir and gate_file are None
    new_root = _auto_correct_root_from_case_dir(args, tmp_path.resolve())
    assert new_root == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# Subprocess integration: full CLI run from a nested workspace cwd
# --------------------------------------------------------------------------- #
def test_subprocess_inspect_self_corrects_from_nested_workspace(tmp_path):
    """End-to-end: scaffold a host project, then invoke the pipeline from a
    nested workspace cwd. The CLI must self-correct ROOT (visible via stderr
    warning) and resolve the workspace to ``<framework>/workspaces/HSP-...``
    rather than the doubly-nested ``<workspace>/workspaces/...`` path."""
    framework = tmp_path / "fw"
    framework.mkdir()

    # Scaffold a real case via the public scaffold entry point so we exercise
    # the same path templates production uses.
    scaffold_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentflow_pipeline.scaffold",
            "--hotspot-name",
            "Cwd Fallback Hotspot",
            "--owner",
            "tester",
            "--root",
            str(framework),
        ],
        cwd=str(framework),
        env=_env_with_src(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert scaffold_proc.returncode == 0, scaffold_proc.stderr

    case_dir = next((framework / "cases").iterdir())

    # Build a nested workspace dir and cd into it -- this is the bug scenario.
    nested_ws = framework / "workspaces" / case_dir.name
    nested_ws.mkdir(parents=True)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentflow_pipeline.cli",
            "--case-dir",
            str(case_dir),
            "--mode",
            "inspect",
        ],
        cwd=str(nested_ws),
        env=_env_with_src(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    # Self-correct warning must land on stderr (machine-readable stdout stays clean).
    assert "auto-corrected ROOT" in proc.stderr, proc.stderr
    assert str(framework) in proc.stderr

    # The plan printout (stdout) must show a workspace under <framework>/workspaces/,
    # NOT the doubly-nested ``<nested_ws>/workspaces/...`` path.
    assert f"Workspace: {framework}/workspaces/" in proc.stdout, proc.stdout
    assert f"Workspace: {nested_ws}/workspaces" not in proc.stdout
