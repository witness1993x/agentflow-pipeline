"""Tests for ``agentflow_pipeline.init_command``.

Covers the bootstrap behaviour expected from ``agentflow-init``:

* fresh-target creation of every artefact
* CLAUDE.md append-vs-overwrite semantics
* idempotency on re-init
* opt-out flags (skip_pool, skip_claude_md)
* auto-mkdir on missing target
* git-repo detection without mutation
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_pipeline.init_command import (
    CLAUDE_MD_SECTION_HEADING,
    init_host_project,
    main,
    summarize_init_actions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_FILES = [
    "cases/README.md",
    "workspaces/README.md",
    "pipeline-pool.md",
    ".agentflow.toml",
    "CLAUDE.md",
]
EXPECTED_DIRS = ["cases", "workspaces"]


def _assert_full_layout(root: Path) -> None:
    for d in EXPECTED_DIRS:
        assert (root / d).is_dir(), f"missing dir: {d}"
    for f in EXPECTED_FILES:
        assert (root / f).is_file(), f"missing file: {f}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_empty_dir_creates_full_layout(tmp_path: Path) -> None:
    """A pristine target gets every expected artefact in one call."""
    target = tmp_path / "host"
    result = init_host_project(target)

    _assert_full_layout(target)
    assert result["errors"] == []
    assert result["target_dir"] == str(target.resolve())
    # Every expected leaf must show up in `created`.
    created_set = set(result["created"])
    for f in EXPECTED_FILES:
        assert str(target / f) in created_set, f"{f} not in created list"
    for d in EXPECTED_DIRS:
        assert str(target / d) + "/" in created_set, f"{d}/ not in created list"


def test_init_target_dir_does_not_exist_is_auto_mkdir(tmp_path: Path) -> None:
    """Missing target with deep parents should be created with mkdir -p."""
    target = tmp_path / "deeply" / "nested" / "host"
    assert not target.exists()

    result = init_host_project(target)

    assert target.is_dir()
    _assert_full_layout(target)
    assert result["errors"] == []


def test_existing_claude_md_is_appended_not_overwritten(tmp_path: Path) -> None:
    """Default behaviour: keep existing CLAUDE.md, append our section."""
    target = tmp_path / "host"
    target.mkdir()
    pre_existing = "# My host project\n\nSome existing memory.\n"
    (target / "CLAUDE.md").write_text(pre_existing, encoding="utf-8")

    result = init_host_project(target)

    body = (target / "CLAUDE.md").read_text(encoding="utf-8")
    # Original prose preserved verbatim
    assert body.startswith("# My host project")
    assert "Some existing memory." in body
    # Our section appended
    assert CLAUDE_MD_SECTION_HEADING in body
    # And the result reports it as appended (not created, not skipped)
    claude_path = str(target / "CLAUDE.md")
    assert claude_path in result["appended"]
    assert claude_path not in result["created"]
    assert claude_path not in result["skipped"]


def test_existing_claude_md_with_force_is_overwritten(tmp_path: Path) -> None:
    """`force=True` replaces an existing CLAUDE.md instead of appending."""
    target = tmp_path / "host"
    target.mkdir()
    pre_existing = "# Soon to be gone\n\nDelete me.\n"
    (target / "CLAUDE.md").write_text(pre_existing, encoding="utf-8")

    result = init_host_project(target, force=True)

    body = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Delete me." not in body
    assert "Soon to be gone" not in body
    assert CLAUDE_MD_SECTION_HEADING in body
    assert str(target / "CLAUDE.md") in result["created"]


def test_skip_claude_md_leaves_file_untouched(tmp_path: Path) -> None:
    """`skip_claude_md=True` neither creates nor modifies CLAUDE.md."""
    target = tmp_path / "host"
    target.mkdir()
    original = "# Untouchable\n"
    claude = target / "CLAUDE.md"
    claude.write_text(original, encoding="utf-8")

    result = init_host_project(target, skip_claude_md=True)

    assert claude.read_text(encoding="utf-8") == original
    claude_str = str(claude)
    assert claude_str not in result["created"]
    assert claude_str not in result["appended"]
    assert claude_str not in result["skipped"]


def test_skip_pool_does_not_create_pool_md(tmp_path: Path) -> None:
    """`skip_pool=True` suppresses pipeline-pool.md creation."""
    target = tmp_path / "host"
    result = init_host_project(target, skip_pool=True)

    assert not (target / "pipeline-pool.md").exists()
    pool_str = str(target / "pipeline-pool.md")
    assert pool_str not in result["created"]
    assert pool_str not in result["skipped"]
    # Other artefacts still present
    assert (target / ".agentflow.toml").is_file()
    assert (target / "cases" / "README.md").is_file()


def test_repeat_init_is_idempotent(tmp_path: Path) -> None:
    """Running init twice on the same dir should be a no-op the second time."""
    target = tmp_path / "host"
    init_host_project(target)
    # Capture content snapshots
    snapshots = {f: (target / f).read_text(encoding="utf-8") for f in EXPECTED_FILES}

    second = init_host_project(target)

    # Nothing new created or appended
    assert second["created"] == [], second["created"]
    assert second["appended"] == [], second["appended"]
    assert second["errors"] == []
    # Every file/dir we care about should be in `skipped`
    skipped_set = set(second["skipped"])
    for f in EXPECTED_FILES:
        assert str(target / f) in skipped_set, f"{f} not skipped"
    for d in EXPECTED_DIRS:
        assert str(target / d) + "/" in skipped_set, f"{d}/ not skipped"
    # Content is byte-identical
    for f, original in snapshots.items():
        assert (target / f).read_text(encoding="utf-8") == original


def test_init_in_git_repo_does_not_touch_git_dir(tmp_path: Path) -> None:
    """A pre-existing `.git` directory must be left strictly alone."""
    target = tmp_path / "host"
    target.mkdir()
    git_dir = target / ".git"
    git_dir.mkdir()
    sentinel = git_dir / "HEAD"
    sentinel.write_text("ref: refs/heads/main\n", encoding="utf-8")
    config = git_dir / "config"
    config.write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")

    result = init_host_project(target)

    # Our artefacts should still be created normally
    _assert_full_layout(target)
    assert result["errors"] == []
    # .git intact, untouched
    assert git_dir.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert config.read_text(encoding="utf-8") == (
        "[core]\n\trepositoryformatversion = 0\n"
    )
    # And nothing in our result mentions .git
    for entry in (
        result["created"] + result["skipped"] + result["appended"] + result["errors"]
    ):
        assert ".git" not in entry or entry.endswith(".agentflow.toml"), (
            f"unexpected .git mention: {entry}"
        )


# ---------------------------------------------------------------------------
# Bonus: format/contents sanity + entry-point behaviours
# ---------------------------------------------------------------------------

def test_agentflow_toml_records_root_and_version(tmp_path: Path) -> None:
    """The generated .agentflow.toml should be parseable and carry metadata."""
    import tomllib

    target = tmp_path / "host"
    init_host_project(target)
    data = tomllib.loads((target / ".agentflow.toml").read_text(encoding="utf-8"))

    assert data["agentflow"]["agentflow_root"] == str(target.resolve())
    assert data["agentflow"]["framework_version"]
    assert data["layout"]["cases_dir"] == "cases"
    assert data["layout"]["pool_file"] == "pipeline-pool.md"


def test_pool_md_starts_with_table_header(tmp_path: Path) -> None:
    """pipeline-pool.md must be a usable empty markdown table."""
    target = tmp_path / "host"
    init_host_project(target)
    body = (target / "pipeline-pool.md").read_text(encoding="utf-8")
    assert body.startswith("# Pipeline Pool")
    assert "| ID |" in body
    # Header separator row present
    assert "|---|" in body


def test_summarize_init_actions_is_compact(tmp_path: Path) -> None:
    """summarize_init_actions returns 2-4 lines, mentions the target dir."""
    target = tmp_path / "host"
    result = init_host_project(target)
    summary = summarize_init_actions(result)
    lines = summary.splitlines()
    assert 2 <= len(lines) <= 4, f"expected 2-4 lines, got {len(lines)}: {summary}"
    assert str(target.resolve()) in summary


def test_main_entry_point_zero_on_success(tmp_path: Path, capsys) -> None:
    """`main()` with positional target dir returns 0 and prints summary."""
    target = tmp_path / "host-from-main"
    rc = main([str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "agentflow-init" in out
    _assert_full_layout(target)


def test_main_entry_point_with_flags(tmp_path: Path, capsys) -> None:
    """`main()` honours --skip-pool and --skip-claude-md flags."""
    target = tmp_path / "host-flags"
    rc = main([str(target), "--skip-pool", "--skip-claude-md"])
    assert rc == 0
    assert not (target / "pipeline-pool.md").exists()
    assert not (target / "CLAUDE.md").exists()
    # But the rest is still scaffolded
    assert (target / "cases" / "README.md").is_file()
    assert (target / ".agentflow.toml").is_file()
