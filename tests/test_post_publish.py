"""Tests for post_publish.apply_post_publish_templates."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_pipeline.post_publish import apply_post_publish_templates, summarize_post_publish_actions


def _config() -> dict:
    return {
        "repo_meta": {"default_branch": "main", "language": "Python"},
        "gate_4_buildability": {
            "build_commands": {
                "install": "pip install -e .",
                "build": "python -m build",
                "test": "pytest -q",
            }
        },
    }


# ---------------------------------------------------------------------------
# apply_post_publish_templates
# ---------------------------------------------------------------------------

class TestApplyPostPublishTemplates:
    def test_first_apply_copies_files(self, tmp_path: Path) -> None:
        workspace = tmp_path / "demo-repo"
        workspace.mkdir()
        result = apply_post_publish_templates(
            workspace,
            _config(),
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )
        assert isinstance(result["copied"], list)
        assert len(result["copied"]) >= 5, result["copied"]
        assert result["skipped"] == []
        # Check at least one expected file actually exists.
        assert (workspace / ".github" / "workflows" / "ci.yml").exists()

    def test_second_apply_is_idempotent(self, tmp_path: Path) -> None:
        workspace = tmp_path / "demo-repo"
        workspace.mkdir()
        first = apply_post_publish_templates(
            workspace,
            _config(),
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )
        second = apply_post_publish_templates(
            workspace,
            _config(),
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )
        assert second["copied"] == [], "no new files should be copied on the second apply"
        assert sorted(second["skipped"]) == sorted(first["copied"]), (
            "the second apply should skip exactly the files copied by the first"
        )

    def test_github_actions_expression_preserved(self, tmp_path: Path) -> None:
        """GitHub Actions ``${{ matrix.os }}`` syntax must not be mangled."""
        workspace = tmp_path / "demo-repo"
        workspace.mkdir()
        apply_post_publish_templates(
            workspace,
            _config(),
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )
        ci = (workspace / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "${{ matrix.os }}" in ci, "GH Actions expression was rewritten"
        assert "${{ matrix.runtime }}" in ci

    def test_known_placeholders_substituted(self, tmp_path: Path) -> None:
        workspace = tmp_path / "demo-repo"
        workspace.mkdir()
        apply_post_publish_templates(
            workspace,
            _config(),
            repo_name="demo-repo",
            github_owner="alice-org",
            language="python",
        )
        codeowners = (workspace / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        assert "{{ github_owner }}" not in codeowners
        # Owner placeholder should now be rendered into the file (mention or path).
        assert "alice-org" in codeowners
        ci = (workspace / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "{{ install_command }}" not in ci
        assert "pip install -e ." in ci

    def test_existing_file_not_overwritten(self, tmp_path: Path) -> None:
        workspace = tmp_path / "demo-repo"
        workspace.mkdir()
        gh_dir = workspace / ".github"
        gh_dir.mkdir()
        existing = gh_dir / "PULL_REQUEST_TEMPLATE.md"
        existing.write_text("EXISTING CUSTOM TEMPLATE", encoding="utf-8")
        result = apply_post_publish_templates(
            workspace,
            _config(),
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )
        assert ".github/PULL_REQUEST_TEMPLATE.md" in result["skipped"]
        assert existing.read_text(encoding="utf-8") == "EXISTING CUSTOM TEMPLATE"

    def test_missing_workspace_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            apply_post_publish_templates(
                tmp_path / "does-not-exist",
                _config(),
                repo_name="demo",
                github_owner="alice",
                language="python",
            )


# ---------------------------------------------------------------------------
# summarize_post_publish_actions
# ---------------------------------------------------------------------------

class TestSummarizePostPublishActions:
    def test_summary_mentions_owner_repo_branch(self) -> None:
        result = {
            "copied": ["a", "b"],
            "skipped": [],
            "rendered_vars": {
                "github_owner": "alice",
                "repo_name": "demo",
                "default_branch": "main",
            },
        }
        text = summarize_post_publish_actions(result)
        assert "alice/demo" in text
        assert "main" in text
        assert "copied 2" in text
