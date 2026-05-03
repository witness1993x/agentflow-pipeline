#!/usr/bin/env python3
"""Scaffold a new Hotspot To GitHub pipeline workspace from templates."""

from __future__ import annotations

import argparse
import os
import re
from datetime import date, timedelta
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"


def _resolve_root(explicit: str | None = None) -> Path:
    """Resolve the host-project root directory.

    Priority (highest -> lowest):
        1. ``explicit`` argument (typically the ``--root`` CLI flag)
        2. ``AGENTFLOW_ROOT`` environment variable
        3. ``Path.cwd()`` (the directory the command was launched from)
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_value = os.environ.get("AGENTFLOW_ROOT", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path.cwd().resolve()


ROOT = _resolve_root()
DEFAULT_POOL_FILE = ROOT / "pipeline-pool.md"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "hotspot"


def replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise ValueError(f"Expected template marker not found: {old!r}")
    return text.replace(old, new, 1)


def render_intake(
    template: str,
    hotspot_id: str,
    hotspot_name: str,
    owner: str,
    today: str,
    question: str,
    project_shape: str,
    repo_strategy: str,
    status: str,
) -> str:
    text = template
    text = replace_once(text, "- Hotspot ID:", f"- Hotspot ID: {hotspot_id}")
    text = replace_once(text, "- Hotspot name:", f"- Hotspot name: {hotspot_name}")
    text = replace_once(text, "- Date:", f"- Date: {today}")
    text = replace_once(text, "- Owner:", f"- Owner: {owner}")
    text = replace_once(text, "- One-line hotspot:", f"- One-line hotspot: {question}")
    text = replace_once(
        text,
        "- Expected project shape: [undecided / demo / starter / sdk / cli / agent_workflow / mcp_server / analytics_dashboard / data_pipeline / alert_bot / graphql_starter / market_monitor]",
        f"- Expected project shape: {project_shape}",
    )
    text = replace_once(
        text,
        "- Initial repo strategy: [undecided / fork_existing / template_clone / new_repo]",
        f"- Initial repo strategy: {repo_strategy}",
    )
    text = replace_once(
        text,
        "- Initial recommendation: [watch / open gate round / drop]",
        f"- Initial recommendation: {status}",
    )
    return text


def render_gate_yaml(
    template: str,
    hotspot_id: str,
    hotspot_name: str,
    owner: str,
    today: str,
    question: str,
    status: str,
    project_shape: str,
    repo_strategy: str,
    thesis: str,
    next_review_date: str,
) -> str:
    text = template
    text = replace_once(text, '  hotspot_id: ""', f'  hotspot_id: "{hotspot_id}"')
    text = replace_once(text, '  hotspot_name: ""', f'  hotspot_name: "{hotspot_name}"')
    text = replace_once(text, '  owner: ""', f'  owner: "{owner}"')
    text = replace_once(text, '  date: ""', f'  date: "{today}"')
    text = replace_once(
        text,
        '  status: "draft" # draft | watch | probe | publish | drop',
        f'  status: "{status}" # draft | watch | probe | publish | drop',
    )
    text = replace_once(text, '  original_question: ""', f'  original_question: "{question}"')
    text = replace_once(
        text,
        '  project_shape: "undecided" # undecided | demo | starter | sdk | cli | agent_workflow | mcp_server | analytics_dashboard | data_pipeline | alert_bot | graphql_starter | market_monitor',
        f'  project_shape: "{project_shape}" # undecided | demo | starter | sdk | cli | agent_workflow | mcp_server | analytics_dashboard | data_pipeline | alert_bot | graphql_starter | market_monitor',
    )
    text = replace_once(
        text,
        '  repo_strategy: "undecided" # undecided | fork_existing | template_clone | new_repo',
        f'  repo_strategy: "{repo_strategy}" # undecided | fork_existing | template_clone | new_repo',
    )
    text = replace_once(
        text,
        '  final_status: "watch" # watch | probe | publish | drop',
        f'  final_status: "{status}" # watch | probe | publish | drop',
    )
    if thesis:
        text = replace_once(text, '  one_line_thesis: ""', f'  one_line_thesis: "{thesis}"')
    if next_review_date:
        text = replace_once(text, '  next_review_date: ""', f'  next_review_date: "{next_review_date}"')
    return text


def render_memo(
    template: str,
    hotspot_id: str,
    hotspot_name: str,
    question: str,
    status: str,
    project_shape: str,
    repo_strategy: str,
    thesis: str,
    next_review_date: str,
) -> str:
    text = template
    text = replace_once(text, "`Decision`: [watch / probe / publish / drop]", f"`Decision`: `{status}`")
    if thesis:
        text = replace_once(text, "`One-line thesis`: [一句话写清楚为什么]", f"`One-line thesis`: {thesis}")
    text = replace_once(text, "- Hotspot ID:", f"- Hotspot ID: {hotspot_id}")
    text = replace_once(text, "- Hotspot:", f"- Hotspot: {hotspot_name}")
    text = replace_once(text, "- Original question:", f"- Original question: {question}")
    text = replace_once(text, "- Project shape:", f"- Project shape: {project_shape}")
    text = replace_once(
        text,
        "- Repo strategy: [fork_existing / template_clone / new_repo]",
        f"- Repo strategy: {repo_strategy}",
    )
    if next_review_date:
        text = replace_once(text, "`Next review date`:", f"`Next review date`: {next_review_date}")
    return text


def render_probe(
    template: str,
    hotspot_id: str,
    hotspot_name: str,
    owner: str,
    today: str,
    repo_strategy: str,
) -> str:
    text = template
    text = replace_once(text, "- Hotspot ID:", f"- Hotspot ID: {hotspot_id}")
    text = replace_once(text, "- Hotspot:", f"- Hotspot: {hotspot_name}")
    text = replace_once(text, "- Date:", f"- Date: {today}")
    text = replace_once(text, "- Owner:", f"- Owner: {owner}")
    text = replace_once(text, "- Repo strategy:", f"- Repo strategy: {repo_strategy}")
    return text


def render_review(
    template: str,
    hotspot_id: str,
    hotspot_name: str,
    owner: str,
    status: str,
    project_shape: str,
    repo_strategy: str,
) -> str:
    text = template
    text = replace_once(text, "- Hotspot ID:", f"- Hotspot ID: {hotspot_id}")
    text = replace_once(text, "- Hotspot:", f"- Hotspot: {hotspot_name}")
    text = replace_once(text, "- Reviewer:", f"- Reviewer: {owner}")
    text = replace_once(text, "- Previous status:", f"- Previous status: {status}")
    text = replace_once(text, "- New status:", f"- New status: {status}")
    text = replace_once(text, "- Updated project shape:", f"- Updated project shape: {project_shape}")
    text = replace_once(text, "- Updated repo strategy:", f"- Updated repo strategy: {repo_strategy}")
    return text


def build_case_files(
    case_dir: Path,
    hotspot_id: str,
    hotspot_name: str,
    owner: str,
    today: str,
    question: str,
    status: str,
    project_shape: str,
    repo_strategy: str,
    thesis: str,
    next_review_date: str,
) -> None:
    intake_template = (TEMPLATES_DIR / "hotspot-intake.template.md").read_text(encoding="utf-8")
    gate_template = (TEMPLATES_DIR / "pipeline-gate.template.yaml").read_text(encoding="utf-8")
    memo_template = (TEMPLATES_DIR / "publish-decision-memo.template.md").read_text(encoding="utf-8")
    probe_template = (TEMPLATES_DIR / "build-probe-run.template.md").read_text(encoding="utf-8")
    review_template = (TEMPLATES_DIR / "review-checkpoint.template.md").read_text(encoding="utf-8")

    files = {
        "01-hotspot-intake.md": render_intake(
            intake_template,
            hotspot_id,
            hotspot_name,
            owner,
            today,
            question,
            project_shape,
            repo_strategy,
            status,
        ),
        "02-pipeline-gate.yaml": render_gate_yaml(
            gate_template,
            hotspot_id,
            hotspot_name,
            owner,
            today,
            question,
            status,
            project_shape,
            repo_strategy,
            thesis,
            next_review_date,
        ),
        "03-publish-decision-memo.md": render_memo(
            memo_template,
            hotspot_id,
            hotspot_name,
            question,
            status,
            project_shape,
            repo_strategy,
            thesis,
            next_review_date,
        ),
        "04-build-probe-run.md": render_probe(
            probe_template, hotspot_id, hotspot_name, owner, today, repo_strategy
        ),
        "05-review-checkpoint.md": render_review(
            review_template, hotspot_id, hotspot_name, owner, status, project_shape, repo_strategy
        ),
    }

    case_dir.mkdir(parents=True, exist_ok=False)
    for filename, content in files.items():
        (case_dir / filename).write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a new Hotspot To GitHub pipeline workspace."
    )
    parser.add_argument(
        "--root",
        default="",
        help=(
            "Host project root. Overrides AGENTFLOW_ROOT and cwd. "
            "Default --output-dir and --pool-file are resolved relative to this."
        ),
    )
    parser.add_argument("--hotspot-name", required=True, help="Hotspot name")
    parser.add_argument("--owner", default="founder", help="Owner or reviewer name")
    parser.add_argument(
        "--question",
        help='Original question to seed the templates. Default: "Should we turn <hotspot> into a GitHub project?"',
    )
    parser.add_argument(
        "--slug",
        help="Optional output slug. Defaults to a slugified form of the hotspot name.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help=(
            "Directory where the case folder will be created. "
            "Default: <root>/cases (where <root> = --root or AGENTFLOW_ROOT or cwd)."
        ),
    )
    parser.add_argument(
        "--status",
        default="watch",
        choices=["draft", "watch", "probe", "publish", "drop"],
        help="Initial status for the new pipeline run.",
    )
    parser.add_argument(
        "--repo-strategy",
        default="undecided",
        choices=["undecided", "fork_existing", "template_clone", "new_repo"],
        help="Initial repo routing strategy.",
    )
    parser.add_argument(
        "--project-shape",
        default="undecided",
        choices=[
            "undecided",
            "demo",
            "starter",
            "sdk",
            "cli",
            "agent_workflow",
            "mcp_server",
            "analytics_dashboard",
            "data_pipeline",
            "alert_bot",
            "graphql_starter",
            "market_monitor",
        ],
        help="Initial project shape.",
    )
    parser.add_argument(
        "--thesis",
        default="",
        help="Optional one-line thesis to seed the memo and pool.",
    )
    parser.add_argument(
        "--next-review-date",
        default="",
        help="Optional next review date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--review-days",
        type=int,
        default=7,
        help="Fallback review window in days when --next-review-date is omitted.",
    )
    parser.add_argument(
        "--pool-file",
        default="",
        help=(
            "Pipeline pool markdown file to update. "
            "Default: <root>/pipeline-pool.md."
        ),
    )
    parser.add_argument(
        "--skip-pool",
        action="store_true",
        help="Do not append the new hotspot to the pipeline pool file.",
    )
    return parser.parse_args()


def ensure_pool_file(pool_file: Path) -> None:
    if pool_file.exists():
        return
    template = (TEMPLATES_DIR / "pipeline-pool.template.md").read_text(encoding="utf-8")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(template, encoding="utf-8")


def next_hotspot_id(pool_file: Path) -> str:
    ensure_pool_file(pool_file)
    text = pool_file.read_text(encoding="utf-8")
    numbers = [int(match.group(1)) for match in re.finditer(r"\bHSP-(\d{3,})\b", text)]
    next_number = (max(numbers) + 1) if numbers else 1
    return f"HSP-{next_number:03d}"


def append_pool_entry(
    pool_file: Path,
    hotspot_id: str,
    hotspot_name: str,
    owner: str,
    status: str,
    project_shape: str,
    repo_strategy: str,
    last_review: str,
    next_review_date: str,
    case_folder: str,
    thesis: str,
) -> None:
    ensure_pool_file(pool_file)
    row = (
        f"| {hotspot_id} | {hotspot_name} | {owner} | {status} | {project_shape} | "
        f"{repo_strategy} | {last_review} | {next_review_date} | `{case_folder}` | {thesis} |\n"
    )
    with pool_file.open("a", encoding="utf-8") as handle:
        handle.write(row)


def resolve_next_review_date(args: argparse.Namespace, today: date) -> str:
    if args.next_review_date:
        return args.next_review_date
    if args.status in {"watch", "probe", "publish"}:
        return (today + timedelta(days=args.review_days)).isoformat()
    return ""


def display_case_folder(case_dir: Path) -> str:
    try:
        return case_dir.relative_to(ROOT).as_posix()
    except ValueError:
        return case_dir.as_posix()


def main() -> int:
    args = parse_args()
    # Re-resolve ROOT once --root is parsed so display logic and lazy defaults
    # all see the user-supplied root.
    global ROOT, DEFAULT_POOL_FILE
    ROOT = _resolve_root(getattr(args, "root", "") or None)
    DEFAULT_POOL_FILE = ROOT / "pipeline-pool.md"
    today_obj = date.today()
    today = today_obj.isoformat()
    slug = args.slug or slugify(args.hotspot_name)
    question = args.question or f"Should we turn {args.hotspot_name} into a GitHub project?"
    next_review_date = resolve_next_review_date(args, today_obj)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else (ROOT / "cases").resolve()
    )
    pool_file = (
        Path(args.pool_file).expanduser().resolve()
        if args.pool_file
        else DEFAULT_POOL_FILE
    )
    hotspot_id = next_hotspot_id(pool_file)
    case_dir = output_dir / f"{hotspot_id}-{today}-{slug}"

    build_case_files(
        case_dir,
        hotspot_id,
        args.hotspot_name,
        args.owner,
        today,
        question,
        args.status,
        args.project_shape,
        args.repo_strategy,
        args.thesis,
        next_review_date,
    )
    if not args.skip_pool:
        append_pool_entry(
            pool_file,
            hotspot_id,
            args.hotspot_name,
            args.owner,
            args.status,
            args.project_shape,
            args.repo_strategy,
            today,
            next_review_date,
            display_case_folder(case_dir),
            args.thesis,
        )
    print(case_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
