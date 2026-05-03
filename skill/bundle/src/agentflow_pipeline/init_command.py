#!/usr/bin/env python3
"""``agentflow-init`` — bootstrap a host project tree.

This is an independent console-script entry point (separate from
``agentflow_pipeline.cli``). It scaffolds the on-disk layout a host
project needs in order to use the framework:

    <target>/
        cases/                  (+ README.md)
        workspaces/             (+ README.md)
        pipeline-pool.md
        .agentflow.toml
        CLAUDE.md               (created or appended)

Existing files are preserved unless ``--force`` is passed; ``CLAUDE.md``
gets a section *appended* when it already exists, instead of being
overwritten — so users who already have project memory don't lose it.

Pure stdlib, no third-party deps.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


# ---------------------------------------------------------------------------
# Constants & template lookups
# ---------------------------------------------------------------------------

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"

CLAUDE_MD_SECTION_HEADING = "## AgentFlow Pipeline"

_FALLBACK_POOL_MD = (
    "# Pipeline Pool\n"
    "\n"
    "| ID | Hotspot | Owner | Status | Project Shape | Repo Strategy "
    "| Last Review | Next Review | Case Folder | One-line Note |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)

_DEFAULT_CASES_README = (
    "# Cases\n"
    "\n"
    "这个目录存放通过 `agentflow-scaffold` 生成的 case 工作副本。\n"
    "\n"
    "建议目录命名保持：\n"
    "\n"
    "- `HSP-001-YYYY-MM-DD-slug`\n"
    "\n"
    "单个 case 仍沿用底层 pipeline 的五件套：\n"
    "\n"
    "- `01-hotspot-intake.md`\n"
    "- `02-pipeline-gate.yaml`\n"
    "- `03-publish-decision-memo.md`\n"
    "- `04-build-probe-run.md`\n"
    "- `05-review-checkpoint.md`\n"
)

_DEFAULT_WORKSPACES_README = (
    "# Workspaces\n"
    "\n"
    "这个目录存放 `agentflow-pipeline` 在 `probe` 或 `publish` 阶段产生的本地工作区。\n"
    "\n"
    "使用规则：\n"
    "\n"
    "- 默认每个 case 对应一个 workspace\n"
    "- 发现同名 workspace 已存在时，执行层会直接报错，避免覆盖\n"
    "- 这里适合放 clone 下来的上游仓库，或 `new_repo` 初始化出的本地仓库\n"
)


def _claude_md_section(framework_version: str) -> str:
    """Render the ``## AgentFlow Pipeline`` block injected into CLAUDE.md."""
    return (
        f"{CLAUDE_MD_SECTION_HEADING}\n"
        "\n"
        "This repository is an **agentflow-pipeline host project**.\n"
        "\n"
        f"- Framework version: `{framework_version}`\n"
        "- Source of truth: the `cases/` directory. Each case is a folder "
        "named `HSP-NNN-YYYY-MM-DD-slug/` and contains five artefacts:\n"
        "\n"
        "  1. `01-hotspot-intake.md`\n"
        "  2. `02-pipeline-gate.yaml`\n"
        "  3. `03-publish-decision-memo.md`\n"
        "  4. `04-build-probe-run.md`\n"
        "  5. `05-review-checkpoint.md`\n"
        "\n"
        "- `workspaces/` holds clones / new repos created during `probe` or "
        "`publish`. Treat them as scratch.\n"
        "- `pipeline-pool.md` is an append-only index of every active case.\n"
        "- `.agentflow.toml` records framework metadata for tooling — do not "
        "hand-edit unless you know why.\n"
        "\n"
        "### Common commands\n"
        "\n"
        "```bash\n"
        "# Create a new case\n"
        "agentflow-scaffold --hotspot-name \"my-hotspot\" --owner alice\n"
        "\n"
        "# Discover candidate repos for a case\n"
        "agentflow-pipeline --mode discover --case-dir cases/HSP-001-...\n"
        "\n"
        "# Probe ChainStream data fit (consumes credits)\n"
        "agentflow-pipeline --mode data-probe --case-dir cases/HSP-001-...\n"
        "\n"
        "# Local build/test probe (no network publish)\n"
        "agentflow-pipeline --mode probe --case-dir cases/HSP-001-...\n"
        "\n"
        "# Publish to GitHub (NOT REVERSIBLE)\n"
        "agentflow-pipeline --mode publish --case-dir cases/HSP-001-...\n"
        "```\n"
        "\n"
        "### Safety notes\n"
        "\n"
        "- `--mode publish` creates / pushes to a real remote GitHub repo. "
        "It is **not** reversible — review the publish-decision memo first.\n"
        "- `--mode data-probe` calls ChainStream APIs and **consumes credits**. "
        "Cache or dry-run when iterating.\n"
        "- Editing `02-pipeline-gate.yaml` directly is fine; keep it the "
        "single source of state for that case.\n"
        "\n"
        "### Reference\n"
        "\n"
        "- Framework README & FRAMEWORK_SPEC live in the installed "
        "`agentflow_pipeline` package. See "
        "<https://github.com/witness1993x/agentflow-pipeline> for upstream docs.\n"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_framework_version() -> str:
    """Best-effort lookup of the installed framework version string."""
    try:
        from . import __version__ as _v  # type: ignore[attr-defined]
        return str(_v)
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _read_template_pool_md() -> str:
    """Return the pipeline-pool template body, or a hardcoded fallback."""
    candidate = TEMPLATES_DIR / "pipeline-pool.template.md"
    if candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            pass
    return _FALLBACK_POOL_MD


def _read_existing_cases_readme() -> str:
    """Try to copy the framework's own cases/README.md as the seed.

    Falls back to a built-in default when the framework is installed in a
    site-packages tree that doesn't ship the dev-time ``cases/`` folder.
    """
    repo_root_candidate = PACKAGE_DIR.parent.parent.parent / "cases" / "README.md"
    if repo_root_candidate.is_file():
        try:
            return repo_root_candidate.read_text(encoding="utf-8")
        except OSError:
            pass
    return _DEFAULT_CASES_README


def _read_existing_workspaces_readme() -> str:
    """Try to copy the framework's own workspaces/README.md as the seed."""
    repo_root_candidate = (
        PACKAGE_DIR.parent.parent.parent / "workspaces" / "README.md"
    )
    if repo_root_candidate.is_file():
        try:
            return repo_root_candidate.read_text(encoding="utf-8")
        except OSError:
            pass
    return _DEFAULT_WORKSPACES_README


def _toml_escape(value: str) -> str:
    """Escape a string for inclusion in a basic TOML double-quoted value."""
    return value.replace("\\", "\\\\").replace("\"", "\\\"")


def _render_agentflow_toml(target_dir: Path, framework_version: str) -> str:
    """Build the ``.agentflow.toml`` body. Handwritten to avoid tomli_w dep."""
    today = date.today().isoformat()
    return (
        "# agentflow-pipeline host project metadata\n"
        "# Auto-generated by `agentflow-init`. Hand-edit at your own risk.\n"
        "\n"
        "[agentflow]\n"
        f"agentflow_root = \"{_toml_escape(str(target_dir))}\"\n"
        f"framework_version = \"{_toml_escape(framework_version)}\"\n"
        f"initialized_at = \"{today}\"\n"
        "config_schema = 1\n"
        "\n"
        "[layout]\n"
        "cases_dir = \"cases\"\n"
        "workspaces_dir = \"workspaces\"\n"
        "pool_file = \"pipeline-pool.md\"\n"
    )


def _write_file(
    path: Path,
    content: str,
    *,
    force: bool,
    created: List[str],
    skipped: List[str],
    errors: List[str],
) -> None:
    """Create ``path`` with ``content``. Skip if exists unless ``force``."""
    rel = str(path)
    try:
        if path.exists() and not force:
            skipped.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(rel)
    except OSError as exc:  # pragma: no cover - filesystem failures
        errors.append(f"{rel}: {exc}")


def _ensure_dir(
    path: Path,
    *,
    created: List[str],
    skipped: List[str],
    errors: List[str],
) -> None:
    rel = str(path) + "/"
    try:
        if path.is_dir():
            skipped.append(rel)
            return
        path.mkdir(parents=True, exist_ok=True)
        created.append(rel)
    except OSError as exc:  # pragma: no cover
        errors.append(f"{rel}: {exc}")


def _handle_claude_md(
    path: Path,
    framework_version: str,
    *,
    force: bool,
    created: List[str],
    skipped: List[str],
    appended: List[str],
    errors: List[str],
) -> None:
    """CLAUDE.md handling has three branches: create, append, or overwrite."""
    rel = str(path)
    section = _claude_md_section(framework_version)
    try:
        if not path.exists():
            path.write_text(section, encoding="utf-8")
            created.append(rel)
            return
        if force:
            path.write_text(section, encoding="utf-8")
            created.append(rel)
            return
        existing = path.read_text(encoding="utf-8")
        if CLAUDE_MD_SECTION_HEADING in existing:
            # Section already present, leave file alone.
            skipped.append(rel)
            return
        # Append section, ensuring blank-line separation.
        sep = "" if existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n"
        )
        path.write_text(existing + sep + section, encoding="utf-8")
        appended.append(rel)
    except OSError as exc:  # pragma: no cover
        errors.append(f"{rel}: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_host_project(
    target_dir: Path,
    *,
    force: bool = False,
    skip_pool: bool = False,
    skip_claude_md: bool = False,
) -> Dict[str, Any]:
    """Bootstrap an agentflow-pipeline host project layout in ``target_dir``.

    Parameters
    ----------
    target_dir:
        Destination root. Will be ``mkdir -p``'d if missing.
    force:
        Overwrite existing files instead of skipping them.
    skip_pool:
        Don't create ``pipeline-pool.md``.
    skip_claude_md:
        Don't touch ``CLAUDE.md`` at all.

    Returns
    -------
    dict
        Keys: ``created``, ``skipped``, ``appended``, ``errors``,
        ``target_dir``. Path entries are stringified absolute paths;
        directory entries end with a trailing ``/``.
    """
    target_dir = Path(target_dir).expanduser().resolve()
    framework_version = _resolve_framework_version()

    created: List[str] = []
    skipped: List[str] = []
    appended: List[str] = []
    errors: List[str] = []

    # 1. Top-level dir
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover
        errors.append(f"{target_dir}: {exc}")
        return {
            "created": created,
            "skipped": skipped,
            "appended": appended,
            "errors": errors,
            "target_dir": str(target_dir),
        }

    # 2. cases/ (+ README)
    cases_dir = target_dir / "cases"
    _ensure_dir(cases_dir, created=created, skipped=skipped, errors=errors)
    _write_file(
        cases_dir / "README.md",
        _read_existing_cases_readme(),
        force=force,
        created=created,
        skipped=skipped,
        errors=errors,
    )

    # 3. workspaces/ (+ README)
    workspaces_dir = target_dir / "workspaces"
    _ensure_dir(workspaces_dir, created=created, skipped=skipped, errors=errors)
    _write_file(
        workspaces_dir / "README.md",
        _read_existing_workspaces_readme(),
        force=force,
        created=created,
        skipped=skipped,
        errors=errors,
    )

    # 4. pipeline-pool.md
    if not skip_pool:
        _write_file(
            target_dir / "pipeline-pool.md",
            _read_template_pool_md(),
            force=force,
            created=created,
            skipped=skipped,
            errors=errors,
        )

    # 5. .agentflow.toml
    _write_file(
        target_dir / ".agentflow.toml",
        _render_agentflow_toml(target_dir, framework_version),
        force=force,
        created=created,
        skipped=skipped,
        errors=errors,
    )

    # 6. CLAUDE.md (create / append / overwrite)
    if not skip_claude_md:
        _handle_claude_md(
            target_dir / "CLAUDE.md",
            framework_version,
            force=force,
            created=created,
            skipped=skipped,
            appended=appended,
            errors=errors,
        )

    return {
        "created": created,
        "skipped": skipped,
        "appended": appended,
        "errors": errors,
        "target_dir": str(target_dir),
    }


def summarize_init_actions(result: Dict[str, Any]) -> str:
    """Return a 2–4 line human-readable summary of an ``init_host_project`` run."""
    target = result.get("target_dir", "?")
    created = result.get("created", []) or []
    skipped = result.get("skipped", []) or []
    appended = result.get("appended", []) or []
    errors = result.get("errors", []) or []

    lines: List[str] = [f"agentflow-init: target={target}"]
    lines.append(
        f"created={len(created)}, appended={len(appended)}, "
        f"skipped={len(skipped)}, errors={len(errors)}"
    )
    if appended:
        lines.append("appended -> " + ", ".join(appended))
    if errors:
        lines.append("errors -> " + "; ".join(errors))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------

def register_init_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach the four init-specific flags to an existing parser.

    Exposed for callers that want to embed init under another subcommand
    tree. The standalone ``main()`` builds its own parser and calls this.
    """
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target directory to bootstrap (default: positional or CWD)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files instead of skipping them",
    )
    parser.add_argument(
        "--skip-pool",
        action="store_true",
        help="Do not create pipeline-pool.md",
    )
    parser.add_argument(
        "--skip-claude-md",
        action="store_true",
        help="Do not create or modify CLAUDE.md",
    )
    return parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentflow-init",
        description=(
            "Bootstrap an agentflow-pipeline host project: cases/, "
            "workspaces/, pipeline-pool.md, .agentflow.toml, CLAUDE.md."
        ),
    )
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=None,
        help="Target directory (positional). Falls back to --target or CWD.",
    )
    register_init_args(parser)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Standalone console-script entry point for ``agentflow-init``."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    target = args.target_dir or args.target or "."
    target_path = Path(target).expanduser().resolve()

    result = init_host_project(
        target_path,
        force=args.force,
        skip_pool=args.skip_pool,
        skip_claude_md=args.skip_claude_md,
    )
    print(summarize_init_actions(result))
    if result.get("errors"):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main())

    # Self-test: idempotent two-pass run inside a tempdir.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "demo-host"
        first = init_host_project(tmp_path)
        expected = [
            tmp_path / "cases",
            tmp_path / "cases" / "README.md",
            tmp_path / "workspaces",
            tmp_path / "workspaces" / "README.md",
            tmp_path / "pipeline-pool.md",
            tmp_path / ".agentflow.toml",
            tmp_path / "CLAUDE.md",
        ]
        missing = [str(p) for p in expected if not p.exists()]
        assert not missing, f"first pass missing: {missing}"
        assert not first["errors"], first["errors"]

        second = init_host_project(tmp_path)
        assert not second["created"], (
            f"second pass should be a no-op, but created: {second['created']}"
        )
        assert not second["appended"], second["appended"]
        assert not second["errors"], second["errors"]

        print("self-test OK")
        print(summarize_init_actions(first))
        print("---")
        print(summarize_init_actions(second))
