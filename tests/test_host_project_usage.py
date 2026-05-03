"""Host-project integration tests for the agentflow-pipeline framework.

Each test runs the CLI in a fresh ``tmp_path`` (acting as a host project)
through ``subprocess.run([sys.executable, "-m", "agentflow_pipeline.cli", ...])``
so the pytest process's ``sys.path`` is **not** polluted by re-imports of the
CLI module that mutate global ROOT state.

These tests prove that:
    1. ``agentflow-scaffold`` writes ``cases/`` into the host project, not the
       framework checkout.
    2. ``agentflow-pipeline`` finds the case in the host project.
    3. ``--root`` and ``AGENTFLOW_ROOT`` are interchangeable.
    4. ``--workspace-root`` overrides the host-project default.
    5. ``--pool-file`` overrides the host-project default.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"


def _env_with_src(extra: dict | None = None) -> dict:
    """Build an env dict that lets the subprocess import ``agentflow_pipeline``.

    We deliberately drop ``AGENTFLOW_ROOT`` from the parent environment so the
    test cases can set it (or not) on purpose.
    """
    env = os.environ.copy()
    env.pop("AGENTFLOW_ROOT", None)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{SRC_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_ROOT)
    )
    if extra:
        env.update(extra)
    return env


def _run_scaffold(*args: str, cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "agentflow_pipeline.scaffold", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=_env_with_src(env_extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _run_pipeline(*args: str, cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "agentflow_pipeline.cli", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=_env_with_src(env_extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --------------------------------------------------------------------------- #
# 1. Scaffold honours --root for cases/ creation
# --------------------------------------------------------------------------- #
def test_scaffold_creates_case_under_root_not_framework(tmp_path: Path) -> None:
    """Running scaffold from outside the framework with --root must drop the
    case into the host project, not into the framework checkout."""
    proc = _run_scaffold(
        "--hotspot-name",
        "Host Test Hotspot",
        "--owner",
        "tester",
        "--root",
        str(tmp_path),
        cwd=tmp_path,
    )
    assert proc.returncode == 0, f"scaffold failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"

    cases_dir = tmp_path / "cases"
    assert cases_dir.is_dir(), f"expected {cases_dir} to exist"

    created = list(cases_dir.iterdir())
    assert len(created) == 1
    case_dir = created[0]
    assert case_dir.name.startswith("HSP-001-")
    assert case_dir.name.endswith("-host-test-hotspot")

    # Pool file is also rooted under tmp_path
    assert (tmp_path / "pipeline-pool.md").exists()

    # Critically: nothing leaked into the framework checkout.
    framework_cases = REPO_ROOT / "cases"
    if framework_cases.exists():
        leaked = [
            p for p in framework_cases.iterdir()
            if p.name.endswith("-host-test-hotspot")
        ]
        assert not leaked, f"case leaked into framework: {leaked}"

    # All five template files were written.
    expected_files = {
        "01-hotspot-intake.md",
        "02-pipeline-gate.yaml",
        "03-publish-decision-memo.md",
        "04-build-probe-run.md",
        "05-review-checkpoint.md",
    }
    actual_files = {p.name for p in case_dir.iterdir()}
    assert expected_files.issubset(actual_files)


# --------------------------------------------------------------------------- #
# 2. Pipeline (inspect mode) finds a host-project case
# --------------------------------------------------------------------------- #
def test_pipeline_inspect_finds_host_project_case(tmp_path: Path) -> None:
    """After scaffolding a case in tmp_path, ``agentflow-pipeline --case-dir``
    in inspect mode loads the gate file successfully."""
    scaffold = _run_scaffold(
        "--hotspot-name",
        "Inspect Hotspot",
        "--owner",
        "tester",
        "--root",
        str(tmp_path),
        cwd=tmp_path,
    )
    assert scaffold.returncode == 0, scaffold.stderr
    case_dir = next((tmp_path / "cases").iterdir())

    proc = _run_pipeline(
        "--root",
        str(tmp_path),
        "--case-dir",
        str(case_dir),
        "--mode",
        "inspect",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, f"inspect failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    # The plan printer dumps hotspot_id and the resolved case folder path.
    assert "HSP-001" in proc.stdout
    assert str(case_dir) in proc.stdout or case_dir.name in proc.stdout


# --------------------------------------------------------------------------- #
# 3. AGENTFLOW_ROOT env var equals --root flag
# --------------------------------------------------------------------------- #
def test_env_var_agentflow_root_equivalent_to_root_flag(tmp_path: Path) -> None:
    """Setting ``AGENTFLOW_ROOT`` in env produces the same result as ``--root``."""
    proc = _run_scaffold(
        "--hotspot-name",
        "Env Root Hotspot",
        "--owner",
        "tester",
        cwd=tmp_path,  # cwd is something else; only env decides ROOT
        env_extra={"AGENTFLOW_ROOT": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr

    cases_dir = tmp_path / "cases"
    assert cases_dir.is_dir()
    created = list(cases_dir.iterdir())
    assert len(created) == 1
    assert created[0].name.startswith("HSP-001-")
    assert (tmp_path / "pipeline-pool.md").exists()


# --------------------------------------------------------------------------- #
# 4. --workspace-root overrides the host-project default
# --------------------------------------------------------------------------- #
def test_workspace_root_explicit_override_wins(tmp_path: Path) -> None:
    """When ``--workspace-root`` is passed, it takes precedence over the
    ROOT-derived default. We assert by inspecting the inspect-mode plan
    printout, which echoes the workspace path."""
    # First, scaffold a case in tmp_path.
    scaffold = _run_scaffold(
        "--hotspot-name",
        "Workspace Override",
        "--owner",
        "tester",
        "--root",
        str(tmp_path),
        cwd=tmp_path,
    )
    assert scaffold.returncode == 0, scaffold.stderr
    case_dir = next((tmp_path / "cases").iterdir())

    explicit_workspace = tmp_path / "alt-workspaces"

    proc = _run_pipeline(
        "--root",
        str(tmp_path),
        "--case-dir",
        str(case_dir),
        "--mode",
        "inspect",
        "--workspace-root",
        str(explicit_workspace),
        cwd=tmp_path,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    # The plan output must reference the explicit workspace, not <root>/workspaces.
    assert "alt-workspaces" in proc.stdout
    assert f"{tmp_path}/workspaces" not in proc.stdout


# --------------------------------------------------------------------------- #
# 5. --pool-file explicit override wins over default
# --------------------------------------------------------------------------- #
def test_pool_file_explicit_override_wins(tmp_path: Path) -> None:
    """``scaffold --pool-file <custom>`` should ignore the ROOT-derived default
    and write the new HSP row into <custom>, not into ``<root>/pipeline-pool.md``."""
    custom_pool = tmp_path / "subdir" / "custom-pool.md"
    proc = _run_scaffold(
        "--hotspot-name",
        "Custom Pool Hotspot",
        "--owner",
        "tester",
        "--root",
        str(tmp_path),
        "--pool-file",
        str(custom_pool),
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr

    assert custom_pool.exists()
    pool_text = custom_pool.read_text(encoding="utf-8")
    assert "HSP-001" in pool_text
    assert "Custom Pool Hotspot" in pool_text

    # The default pool file under root must NOT have been created.
    default_pool = tmp_path / "pipeline-pool.md"
    assert not default_pool.exists(), (
        "default pool file was unexpectedly created when --pool-file was supplied"
    )


# --------------------------------------------------------------------------- #
# 6. --root flag wins over AGENTFLOW_ROOT env var
# --------------------------------------------------------------------------- #
def test_root_flag_overrides_agentflow_root_env(tmp_path: Path) -> None:
    """If both ``AGENTFLOW_ROOT`` and ``--root`` are provided, ``--root`` wins."""
    env_root = tmp_path / "env_root"
    flag_root = tmp_path / "flag_root"
    env_root.mkdir()
    flag_root.mkdir()

    proc = _run_scaffold(
        "--hotspot-name",
        "Priority Hotspot",
        "--owner",
        "tester",
        "--root",
        str(flag_root),
        cwd=tmp_path,
        env_extra={"AGENTFLOW_ROOT": str(env_root)},
    )
    assert proc.returncode == 0, proc.stderr

    # flag_root should contain the case; env_root should be untouched.
    assert (flag_root / "cases").is_dir()
    assert any((flag_root / "cases").iterdir())
    assert not (env_root / "cases").exists(), (
        "AGENTFLOW_ROOT must lose to --root when both are set"
    )
