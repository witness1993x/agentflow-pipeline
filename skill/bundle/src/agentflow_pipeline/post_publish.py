"""Post-publish scaffolding for freshly published repositories.

This module copies and renders the `templates/post-publish/` tree into a
workspace that has just been published by `run_pipeline.py`. It seeds CI,
issue/PR templates, CODEOWNERS, README badge snippets, and a monitoring
playbook so each new repo starts with a baseline of operational hygiene.

Integration patch
-----------------
Add the import near the other top-level imports in `run_pipeline.py`::

    from post_publish import apply_post_publish_templates, summarize_post_publish_actions

Then, inside `main()`, after `publish_workspace(...)` succeeds and BEFORE
`writeback_probe(...)` is called, drop in::

    if args.mode == "publish" and args.execute and args.allow_publish:
        publish_state = ensure_nested_dict(ensure_execution_state(config), "publish")
        post_result = apply_post_publish_templates(
            workspace,
            config,
            repo_name=repo_name(config),
            github_owner=repo_owner(config) or current_gh_login(),
            language=str(config.get("repo_meta", {}).get("language", "")),
        )
        publish_state["post_publish"] = post_result
        print_section("Post-Publish Scaffolding")
        print(summarize_post_publish_actions(post_result))

The result dict is persisted on `execution_state.publish.post_publish` so
downstream writeback / memo steps can reference it.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable


# Ordered so longer / more specific keys win; mostly cosmetic since str.replace
# is unambiguous for these tokens.
_PLACEHOLDER_KEYS = (
    "github_owner",
    "repo_name",
    "install_command",
    "build_command",
    "test_command",
    "default_branch",
    "language",
)


def _templates_root() -> Path:
    return Path(__file__).resolve().parent / "templates" / "post-publish"


def _resolve_command(config: dict, key: str, fallback: str) -> str:
    gate4 = config.get("gate_4_buildability") or {}
    commands = gate4.get("build_commands") or {}
    value = commands.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _resolve_default_branch(config: dict) -> str:
    repo_meta = config.get("repo_meta") or {}
    branch = repo_meta.get("default_branch")
    if isinstance(branch, str) and branch.strip():
        return branch.strip()
    return "main"


def _build_render_vars(
    config: dict,
    *,
    repo_name: str,
    github_owner: str,
    language: str,
) -> dict[str, str]:
    return {
        "github_owner": github_owner or "<github-owner>",
        "repo_name": repo_name or "<repo-name>",
        "install_command": _resolve_command(config, "install", "echo 'configure install command'"),
        "build_command": _resolve_command(config, "build", "echo 'configure build command'"),
        "test_command": _resolve_command(config, "test", "echo 'configure test command'"),
        "default_branch": _resolve_default_branch(config),
        "language": (language or "node").strip().lower() or "node",
    }


def _render_text(text: str, render_vars: dict[str, str]) -> str:
    """Replace `{{ key }}` tokens for keys we know about, leave others intact.

    Crucially this skips GitHub Actions expressions like `${{ matrix.os }}`
    because the `$` prefix never matches our `{{ name }}` form.
    """
    rendered = text
    for key in _PLACEHOLDER_KEYS:
        token = "{{ " + key + " }}"
        if token in rendered:
            rendered = rendered.replace(token, render_vars.get(key, ""))
    return rendered


# Files that are textual and safe to render. Everything in the post-publish
# tree is text, so we just gate on suffix to be defensive.
_TEXT_SUFFIXES = {".md", ".yml", ".yaml", ".txt", ""}


def _iter_template_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _destination_for(template_file: Path, root: Path, workspace: Path) -> Path:
    rel = template_file.relative_to(root)
    parts = list(rel.parts)
    # Map `github/...` in the template tree onto `.github/...` in the workspace.
    if parts and parts[0] == "github":
        parts[0] = ".github"
    return workspace.joinpath(*parts)


def apply_post_publish_templates(
    workspace: Path,
    config: dict,
    *,
    repo_name: str,
    github_owner: str,
    language: str = "",
) -> dict:
    """Copy and render `templates/post-publish/**` into `workspace`.

    Existing files are left untouched. Returns a structured result dict with
    `copied`, `skipped`, and `rendered_vars` for downstream logging / writeback.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise FileNotFoundError(f"workspace does not exist: {workspace}")

    root = _templates_root()
    if not root.exists():
        raise FileNotFoundError(f"post-publish templates missing: {root}")

    render_vars = _build_render_vars(
        config,
        repo_name=repo_name,
        github_owner=github_owner,
        language=language,
    )

    copied: list[str] = []
    skipped: list[str] = []

    for template_file in _iter_template_files(root):
        destination = _destination_for(template_file, root, workspace)
        rel_display = str(destination.relative_to(workspace))

        if destination.exists():
            skipped.append(rel_display)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        suffix = template_file.suffix.lower()
        if suffix in _TEXT_SUFFIXES:
            text = template_file.read_text(encoding="utf-8")
            destination.write_text(_render_text(text, render_vars), encoding="utf-8")
        else:
            shutil.copyfile(template_file, destination)
        copied.append(rel_display)

    return {
        "copied": copied,
        "skipped": skipped,
        "rendered_vars": render_vars,
    }


def summarize_post_publish_actions(result: dict) -> str:
    """Return a 2-4 line human-readable summary of an apply result."""
    copied = result.get("copied") or []
    skipped = result.get("skipped") or []
    rendered_vars = result.get("rendered_vars") or {}
    owner = rendered_vars.get("github_owner", "<github-owner>")
    name = rendered_vars.get("repo_name", "<repo-name>")
    branch = rendered_vars.get("default_branch", "main")

    lines = [
        f"post-publish scaffolding for {owner}/{name} (branch {branch})",
        f"copied {len(copied)} file(s); skipped {len(skipped)} pre-existing file(s)",
    ]
    if copied:
        preview = ", ".join(copied[:5])
        if len(copied) > 5:
            preview += f", +{len(copied) - 5} more"
        lines.append(f"new: {preview}")
    if skipped:
        preview = ", ".join(skipped[:3])
        if len(skipped) > 3:
            preview += f", +{len(skipped) - 3} more"
        lines.append(f"kept: {preview}")
    return "\n".join(lines)


def _self_test() -> int:
    config = {
        "repo_meta": {"default_branch": "main", "language": "Python"},
        "gate_4_buildability": {
            "build_commands": {
                "install": "pip install -e .",
                "build": "python -m build",
                "test": "pytest -q",
            }
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "demo-repo"
        workspace.mkdir()
        # Simulate a pre-existing PR template to test the skip path.
        (workspace / ".github").mkdir()
        (workspace / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(
            "existing content", encoding="utf-8"
        )

        first = apply_post_publish_templates(
            workspace,
            config,
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )
        second = apply_post_publish_templates(
            workspace,
            config,
            repo_name="demo-repo",
            github_owner="alice",
            language="python",
        )

        print("=== first run ===")
        print(summarize_post_publish_actions(first))
        print()
        print("copied:", first["copied"])
        print("skipped:", first["skipped"])
        print("rendered_vars:", first["rendered_vars"])
        print()
        print("=== second run (idempotent) ===")
        print(summarize_post_publish_actions(second))
        print("copied:", second["copied"])
        print("skipped count:", len(second["skipped"]))

        # Spot-check rendered output.
        ci = (workspace / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "{{ install_command }}" not in ci, "install_command not rendered"
        assert "pip install -e ." in ci, "install_command value missing"
        assert "${{ matrix.os }}" in ci, "GitHub Actions expression mangled"
        codeowners = (workspace / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        assert "@alice" in codeowners, "CODEOWNERS owner not rendered"
        pr_template = (workspace / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
        assert pr_template == "existing content", "pre-existing PR template was overwritten"
        print()
        print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
