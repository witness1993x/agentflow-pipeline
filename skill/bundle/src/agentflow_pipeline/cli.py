#!/usr/bin/env python3
"""Execute Hotspot To GitHub pipeline cases with discovery and writeback."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse, request

import yaml

from .auto_publish import register_auto_publish_args, run_auto_publish
from .build_command_inference import auto_fill_build_commands, register_build_inference_args
from .chainstream_query_builder import register_query_builder_args, resolve_probe_query
from .data_source import (
    DataSourceError,
    DataSourcePlugin,
    default_data_source,
    get_data_source,
    registered_data_sources,
)
from .dedup_candidates import dedup_candidates
from .extra_sources import (
    ExtraSourceError,
    extra_sources_arg_helpers,
    hackernews_search,
    normalize_hackernews_candidates,
    normalize_reddit_candidates,
    reddit_search,
    register_extra_sources_args,
)
from .kafka_probe import (
    kafka_probe_args_from_namespace,
    run_chainstream_kafka_probe,
    update_gate_after_kafka_probe,
)
from .monitoring_grafana_pagerduty import (
    register_grafana_pagerduty_args,
    run_external_monitoring,
)
from .monitoring_setup import register_monitoring_args, run_monitoring_setup
from .pool_advancer import (
    format_advance_summary,
    register_advance_args,
    run_pool_auto_advance,
)
from .pool_runner import (
    find_pool_cases,
    format_pool_summary,
    pool_args_to_kwargs,
    register_pool_args,
    run_pool_parallel,
)
from .post_publish import apply_post_publish_templates, summarize_post_publish_actions
from .topics_enrichment import enrich_candidates_with_topics


PACKAGE_DIR = Path(__file__).resolve().parent


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


def _auto_correct_root_from_case_dir(
    args: argparse.Namespace,
    current_root: Path,
) -> Path:
    """Self-correct ROOT when cwd drifted away from the framework root.

    Scenario: a user prepares source code under ``<framework>/workspaces/HSP-X/``,
    runs ``cd workspaces/HSP-X && npm install``, then invokes
    ``agentflow-pipeline --case-dir <framework>/cases/HSP-X --mode publish ...``
    **without** ``cd``-ing back to the framework root.  The default ROOT
    (``Path.cwd()``) silently becomes the workspace path, so
    ``DEFAULT_WORKSPACE_ROOT = ROOT/workspaces`` resolves to
    ``<workspace>/workspaces`` and ``workspace_dir`` becomes a doubly-nested
    path -- causing prepare/probe/publish to operate on an empty mirror folder
    while the user's real source code never gets published.

    This helper looks at ``--case-dir`` / ``--gate-file``: if either points
    inside a ``cases/`` directory and the inferred framework root differs
    from the resolved ROOT, it returns the inferred root and prints a single
    stderr warning so machine-readable JSON output on stdout stays clean.

    Self-correction is **opt-out**:
        * never triggers when ``--root`` was passed explicitly
        * never triggers when ``AGENTFLOW_ROOT`` env var is set
        * never triggers in ``--mode pool`` (no case-dir input)
        * never triggers when the case-dir's parent is not literally ``cases``
          -- preserves backward-compat for users with custom directory layouts

    Args:
        args: parsed argparse namespace
        current_root: ROOT as already resolved by ``_resolve_root``

    Returns:
        New ROOT (may equal ``current_root`` if no correction is needed).
    """
    # 1. Respect explicit user intent: never override --root or AGENTFLOW_ROOT.
    if (getattr(args, "root", "") or "").strip():
        return current_root
    if os.environ.get("AGENTFLOW_ROOT", "").strip():
        return current_root
    # 2. Pool mode has no case-dir input.
    if getattr(args, "mode", "") == "pool":
        return current_root

    # 3. Pick the case directory (preferring --case-dir, falling back to
    #    --gate-file's containing folder).
    case_dir_arg = getattr(args, "case_dir", None)
    gate_file_arg = getattr(args, "gate_file", None)
    case_dir: Path | None = None
    if case_dir_arg:
        case_dir = Path(case_dir_arg).expanduser().resolve()
    elif gate_file_arg:
        case_dir = Path(gate_file_arg).expanduser().resolve().parent

    if case_dir is None:
        return current_root

    # 4. Detect the canonical ``<framework>/cases/<HSP-...>`` layout.
    if case_dir.parent.name != "cases":
        return current_root

    inferred_root = case_dir.parent.parent
    if inferred_root == current_root:
        return current_root

    print(
        f"[agentflow] auto-corrected ROOT: {current_root} -> {inferred_root} "
        f"(inferred from --case-dir)",
        file=sys.stderr,
    )
    return inferred_root


# Module-level ROOT honours env var / cwd at import time. CLI parsing replaces
# it with ``_resolve_root(args.root)`` so that ``--root`` always wins.
ROOT = _resolve_root()
DEFAULT_WORKSPACE_ROOT = ROOT / "workspaces"
DEFAULT_POOL_FILE = ROOT / "pipeline-pool.md"


class PipelineError(RuntimeError):
    """Raised for pipeline execution errors."""


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


REVIEW_CADENCE_DAYS = {
    "passed": 7,
    "pass": 7,
    "ready": 7,
    "blocked": 3,
    "hold": 3,
    "planned": 3,
    "failed": 14,
    "fail": 14,
}


def compute_next_review_date(outcome: str, default_days: int = 7) -> str:
    days = REVIEW_CADENCE_DAYS.get(str(outcome or "").lower(), default_days)
    target = datetime.now(timezone.utc) + timedelta(days=days)
    return target.strftime("%Y-%m-%d")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "hotspot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Hotspot To GitHub pipeline case.")
    parser.add_argument(
        "--root",
        default="",
        help=(
            "Host project root. Overrides AGENTFLOW_ROOT and cwd. "
            "Default workspace-root and pool-file are resolved relative to this."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--case-dir", help="Case directory containing 02-pipeline-gate.yaml")
    group.add_argument("--gate-file", help="Path to a pipeline gate YAML file")
    parser.add_argument(
        "--mode",
        default="inspect",
        choices=["inspect", "discover", "data-probe", "kafka-probe", "probe", "publish", "pool"],
        help="Execution mode. Default: inspect",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run commands and write back results. Without this flag the tool prints a dry-run plan.",
    )
    parser.add_argument(
        "--allow-publish",
        action="store_true",
        help="Required together with --execute when mode=publish.",
    )
    parser.add_argument(
        "--workspace-root",
        default="",
        help=(
            "Directory used for local workspaces. "
            "Default: <root>/workspaces (where <root> = --root or AGENTFLOW_ROOT or cwd)."
        ),
    )
    parser.add_argument(
        "--candidate-index",
        type=int,
        default=0,
        help="Which candidate repo to use for fork_existing/template_clone. Default: 0",
    )
    parser.add_argument(
        "--discover-query",
        default="",
        help="Optional override query used for candidate repo discovery.",
    )
    parser.add_argument(
        "--discover-sources",
        default="github",
        help="Comma-separated discovery sources: github,jina,x,all. Default: github",
    )
    parser.add_argument(
        "--jina-query",
        default="",
        help="Optional Jina search query override. Defaults to --discover-query.",
    )
    parser.add_argument(
        "--x-query",
        default="",
        help="Optional X recent search query override. Defaults to --discover-query.",
    )
    parser.add_argument(
        "--discover-limit",
        type=int,
        default=5,
        help="Max number of candidate repos to discover.",
    )
    parser.add_argument(
        "--chainstream-endpoint",
        default="https://graphql.chainstream.io/graphql",
        help="ChainStream GraphQL endpoint used by data-probe.",
    )
    parser.add_argument(
        "--chainstream-api-key-env",
        default="CHAINSTREAM_API_KEY",
        help="Environment variable containing the ChainStream API key.",
    )
    parser.add_argument(
        "--chainstream-query",
        default="",
        help="Inline GraphQL query used by data-probe.",
    )
    parser.add_argument(
        "--chainstream-query-file",
        default="",
        help="Path to a GraphQL query file used by data-probe.",
    )
    parser.add_argument(
        "--data-source",
        default="",
        help=(
            "Override the active DataSourcePlugin (default reads "
            "$AGENTFLOW_DATA_SOURCE, falling back to 'chainstream'). "
            "Built-ins: " + ", ".join(registered_data_sources()) + "."
        ),
    )
    parser.add_argument(
        "--kafka-bootstrap-servers",
        default="",
        help="Chainstream Kafka bootstrap servers, e.g. kafka.chainstream.io:9093.",
    )
    parser.add_argument(
        "--kafka-topic",
        default="",
        help="Kafka topic to subscribe to during kafka-probe.",
    )
    parser.add_argument(
        "--kafka-sasl-username-env",
        default="CHAINSTREAM_KAFKA_USERNAME",
        help="Env var holding the Kafka SASL/PLAIN username (api key id).",
    )
    parser.add_argument(
        "--kafka-sasl-password-env",
        default="CHAINSTREAM_KAFKA_PASSWORD",
        help="Env var holding the Kafka SASL/PLAIN password (api secret).",
    )
    parser.add_argument(
        "--kafka-timeout-seconds",
        type=int,
        default=10,
        help="Seconds to wait for at least one Kafka message during kafka-probe.",
    )
    parser.add_argument(
        "--kafka-group-id",
        default="chainstream-pipeline-probe",
        help="Kafka consumer group id for the probe consumer.",
    )
    parser.add_argument(
        "--pool-file",
        default="",
        help=(
            "Pipeline pool markdown file to update during writeback. "
            "Default: <root>/pipeline-pool.md."
        ),
    )
    parser.add_argument(
        "--no-writeback",
        action="store_true",
        help="Execute commands but do not persist results back to gate/probe-run/pool files.",
    )
    parser.add_argument(
        "--reuse-existing-workspace",
        action="store_true",
        help="Skip the empty-workspace check; use the existing workspace as-is. Useful when source code was prepared manually before publish.",
    )
    register_auto_publish_args(parser)
    register_extra_sources_args(parser)
    register_monitoring_args(parser)
    register_grafana_pagerduty_args(parser)
    register_pool_args(parser)
    register_advance_args(parser)
    register_build_inference_args(parser)
    register_query_builder_args(parser)
    return parser.parse_args()


def load_gate_file(args: argparse.Namespace) -> tuple[Path, dict]:
    gate_file = Path(args.gate_file).expanduser().resolve() if args.gate_file else (
        Path(args.case_dir).expanduser().resolve() / "02-pipeline-gate.yaml"
    )
    if not gate_file.exists():
        raise PipelineError(f"Gate file not found: {gate_file}")
    with gate_file.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return gate_file, data


def dump_gate_file(path: Path, config: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)


def require_string(mapping: dict, *keys: str) -> str:
    current = mapping
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return ""
        current = current[key]
    return current if isinstance(current, str) else ""


def ensure_nested_dict(mapping: dict, *keys: str) -> dict:
    current = mapping
    for key in keys:
        value = current.get(key)
        if not isinstance(value, dict):
            value = {}
            current[key] = value
        current = value
    return current


def candidate_repo(config: dict, index: int) -> dict:
    items = config.get("gate_3_repo_routing", {}).get("candidate_repos", [])
    if not items:
        return {}
    if index < 0 or index >= len(items):
        raise PipelineError(f"Candidate repo index out of range: {index}")
    item = items[index]
    return item if isinstance(item, dict) else {}


def case_dir_from_gate(gate_file: Path) -> Path:
    return gate_file.parent


def probe_run_file(case_dir: Path) -> Path:
    return case_dir / "04-build-probe-run.md"


def workspace_dir(config: dict, workspace_root: Path) -> Path:
    repo_plan = config.get("repo_plan", {})
    explicit = repo_plan.get("local_workspace", "")
    if explicit:
        return Path(explicit).expanduser().resolve()
    hotspot_id = require_string(config, "meta", "hotspot_id") or "HSP-NEW"
    hotspot_name = require_string(config, "meta", "hotspot_name") or "hotspot"
    date_value = require_string(config, "meta", "date") or "undated"
    return workspace_root.expanduser().resolve() / f"{hotspot_id}-{date_value}-{slugify(hotspot_name)}"


def repo_name(config: dict) -> str:
    explicit = require_string(config, "repo_plan", "repo_name")
    if explicit:
        return explicit
    hotspot_name = require_string(config, "meta", "hotspot_name") or "hotspot"
    return slugify(hotspot_name)


def repo_owner(config: dict) -> str:
    return require_string(config, "repo_plan", "github_owner")


def repo_visibility(config: dict) -> str:
    visibility = require_string(config, "repo_plan", "visibility") or "public"
    if visibility not in {"public", "private"}:
        raise PipelineError(f"Unsupported visibility: {visibility}")
    return visibility


def default_branch(config: dict) -> str:
    return require_string(config, "repo_plan", "default_branch") or "main"


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def ensure_empty_workspace(target: Path) -> None:
    if target.exists():
        raise PipelineError(f"Workspace already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def print_plan(config: dict, gate_file: Path, workspace: Path, candidate: dict) -> None:
    hotspot_id = require_string(config, "meta", "hotspot_id")
    hotspot_name = require_string(config, "meta", "hotspot_name")
    status = require_string(config, "decision", "final_status")
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    print_section("Pipeline Summary")
    print(f"Gate file: {gate_file}")
    print(f"Hotspot: {hotspot_id} {hotspot_name}")
    print(f"Status: {status}")
    print(f"Project shape: {shape}")
    print(f"Repo strategy: {strategy}")
    print(f"Workspace: {workspace}")
    if candidate:
        print(f"Candidate repo: {candidate.get('name', '')} {candidate.get('url', '')}")
        if candidate.get("chainstream_fit_score") is not None:
            print(
                "Chainstream fit: "
                f"{candidate.get('chainstream_fit_score', 0)} "
                f"via {candidate.get('recommended_chainstream_access', 'graphql')}"
            )
            print(f"Fork/build recommendation: {candidate.get('fork_or_build_recommendation', '')}")
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if isinstance(commands, dict):
        print(f"Install command: {commands.get('install', '')}")
        print(f"Build command: {commands.get('build', '')}")
        print(f"Test command: {commands.get('test', '')}")


def dedupe_terms(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = part.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def discovery_query(config: dict, override: str) -> str:
    if override:
        return override
    saved = require_string(config, "gate_3_repo_routing", "discovered_query")
    if saved:
        return saved
    parts = [require_string(config, "meta", "hotspot_name")]
    topic_lineage = config.get("source_context", {}).get("topic_lineage", [])
    if isinstance(topic_lineage, list):
        parts.extend(item for item in topic_lineage if isinstance(item, str))
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    if shape and shape != "undecided":
        parts.append(shape.replace("_", " "))
    return " ".join(dedupe_terms(parts)).strip()


GH_SEARCH_JSON_FIELDS = (
    "name,owner,url,description,stargazersCount,forksCount,openIssuesCount,"
    "updatedAt,pushedAt,isArchived,isFork,language,defaultBranch,homepage,license"
)


def gh_search_repos(query: str, limit: int) -> list[dict]:
    command = [
        "gh",
        "search",
        "repos",
        query,
        "--limit",
        str(limit),
        "--json",
        GH_SEARCH_JSON_FIELDS,
    ]
    result = run_command(command)
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "gh search repos failed")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Unable to parse GitHub search output: {exc}") from exc
    if not isinstance(payload, list):
        raise PipelineError("Unexpected GitHub search payload shape.")
    return payload


def parse_discovery_sources(value: str) -> list[str]:
    requested = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not requested:
        return ["github"]
    if "all" in requested:
        return ["github", "jina", "x", "hackernews", "reddit"]
    allowed = {"github", "jina", "x", "hackernews", "reddit"}
    unknown = [item for item in requested if item not in allowed]
    if unknown:
        raise PipelineError(f"Unsupported discovery source(s): {', '.join(unknown)}")
    return dedupe_terms(requested)


DEFAULT_HTTP_USER_AGENT = "agentflow-git-repo-clone/0.1"


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict | list:
    body = None
    final_headers = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_HTTP_USER_AGENT,
        **(headers or {}),
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=final_headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PipelineError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except error.URLError as exc:
        raise PipelineError(f"Unable to reach {url}: {exc.reason}") from exc
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Unable to parse JSON response from {url}: {exc}") from exc


def resolve_data_source(args: argparse.Namespace | None = None) -> DataSourcePlugin:
    """Pick the active :class:`DataSourcePlugin` for a given CLI invocation.

    Priority: ``--data-source`` flag > ``AGENTFLOW_DATA_SOURCE`` env var >
    built-in default (``chainstream``).

    Translates :class:`DataSourceError` into :class:`PipelineError` so callers
    only need to handle one error type.
    """
    explicit = ""
    if args is not None:
        explicit = (getattr(args, "data_source", "") or "").strip()
    try:
        if explicit:
            return get_data_source(explicit)
        return default_data_source()
    except DataSourceError as exc:
        raise PipelineError(str(exc)) from exc


def post_chainstream_graphql(endpoint: str, api_key: str, query: str) -> dict:
    """Backwards-compat wrapper that dispatches to the default data source.

    Kept for any external caller that imports this name directly. Internal
    pipeline code should prefer ``plugin.post_graphql_probe(...)``.
    """
    plugin = default_data_source()
    try:
        return plugin.post_graphql_probe(endpoint, api_key, query)
    except DataSourceError as exc:
        raise PipelineError(str(exc)) from exc


def jina_search(query: str, limit: int) -> list[dict]:
    headers = {"X-Return-Format": "text", "X-Respond-With": "no-content"}
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = http_json("POST", "https://s.jina.ai/", {"q": query, "num": limit}, headers)
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        data = payload.get("data") or payload.get("results") or payload.get("items") or []
        items = data if isinstance(data, list) else []
    else:
        items = []
    return [item for item in items if isinstance(item, dict)][:limit]


def x_recent_search(query: str, limit: int) -> list[dict]:
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        raise PipelineError("X_BEARER_TOKEN is required for X recent search.")
    max_results = min(max(limit, 10), 100)
    params = parse.urlencode(
        {
            "query": query,
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,lang",
        }
    )
    payload = http_json(
        "GET",
        f"https://api.x.com/2/tweets/search/recent?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [item for item in data if isinstance(item, dict)][:limit]


def normalize_jina_candidates(raw_items: list[dict], query: str, config: dict) -> list[dict]:
    candidates: list[dict] = []
    for item in raw_items:
        title = str(item.get("title") or item.get("name") or item.get("url") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        description = str(
            item.get("description")
            or item.get("content")
            or item.get("snippet")
            or item.get("text")
            or ""
        ).strip()
        candidate = {
            "source": "jina_search",
            "name": title or url or "jina-result",
            "url": url,
            "description": description[:500],
            "stars": 0,
            "updated_at": "",
            "fit_reason": f"Discovered from Jina search query: {query}",
            "license_note": "",
        }
        enrich_candidate(config, candidate)
        candidates.append(candidate)
    return candidates


def normalize_x_candidates(raw_items: list[dict], query: str, config: dict) -> list[dict]:
    candidates: list[dict] = []
    for item in raw_items:
        metrics = item.get("public_metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        engagement = sum(int(metrics.get(key, 0) or 0) for key in ("retweet_count", "reply_count", "like_count", "quote_count"))
        text = str(item.get("text", "") or "").strip()
        tweet_id = str(item.get("id", "") or "").strip()
        candidate = {
            "source": "x_search",
            "name": f"x-post-{tweet_id}" if tweet_id else "x-post",
            "url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "",
            "description": text[:500],
            "stars": engagement,
            "updated_at": str(item.get("created_at", "") or "").strip(),
            "fit_reason": f"Discovered from X recent search query: {query}",
            "license_note": "social_signal",
        }
        enrich_candidate(config, candidate)
        candidates.append(candidate)
    return candidates


def owner_login(item: dict) -> str:
    owner = item.get("owner")
    if isinstance(owner, dict):
        return str(owner.get("login", "")).strip()
    return str(owner or "").strip()


def normalize_license(item: dict) -> str:
    license_info = item.get("license")
    if isinstance(license_info, dict):
        return str(
            license_info.get("spdx_id")
            or license_info.get("spdxId")
            or license_info.get("name")
            or ""
        ).strip()
    return ""


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


CHAINSTREAM_FRIENDLY_LANGUAGES = {
    "typescript", "javascript", "python", "go", "rust", "java", "kotlin",
}


def _activity_age_days(candidate: dict) -> int | None:
    pushed_at = parse_timestamp(str(candidate.get("pushed_at", "")))
    updated_at = parse_timestamp(str(candidate.get("updated_at", "")))
    timestamp = pushed_at or updated_at
    if not timestamp:
        return None
    delta = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
    return max(delta.days, 0)


def score_candidate(config: dict, candidate: dict) -> tuple[int, str, dict]:
    hotspot_slug = slugify(require_string(config, "meta", "hotspot_name"))
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    name_slug = slugify(str(candidate.get("name", "")))
    description_slug = slugify(str(candidate.get("description", "")))
    score = 0
    reasons: list[str] = []
    signals: dict[str, object] = {}

    if candidate.get("is_archived"):
        score -= 40
        reasons.append("已归档，强烈降权")
        signals["archived_penalty"] = -40
    if candidate.get("is_fork"):
        score -= 10
        reasons.append("派生仓库，轻度降权")
        signals["fork_penalty"] = -10

    if hotspot_slug and hotspot_slug in name_slug:
        score += 30
        reasons.append("名称贴近热点")
        signals["hotspot_name_match"] = 30
    if shape != "undecided":
        shape_token = shape.replace("_", "-")
        if shape_token in name_slug or shape_token in description_slug:
            score += 20
            reasons.append("项目形态匹配")
            signals["shape_match"] = 20

    stars = int(candidate.get("stars", 0) or 0)
    star_score = 0
    if stars >= 1000:
        star_score = 22
        reasons.append("stars 很高")
    elif stars >= 500:
        star_score = 20
        reasons.append("stars 高")
    elif stars >= 100:
        star_score = 15
        reasons.append("stars 较高")
    elif stars >= 20:
        star_score = 10
        reasons.append("有基础社区验证")
    if star_score:
        score += star_score
        signals["stars_score"] = star_score

    forks = int(candidate.get("forks", 0) or 0)
    if forks >= 200:
        score += 8
        reasons.append("被广泛 fork")
        signals["forks_score"] = 8
    elif forks >= 50:
        score += 5
        reasons.append("有一定 fork 量")
        signals["forks_score"] = 5
    elif forks >= 10:
        score += 2
        signals["forks_score"] = 2

    open_issues = int(candidate.get("open_issues", 0) or 0)
    if open_issues >= 5 and stars >= 50:
        score += 4
        reasons.append("issue 活跃")
        signals["issues_active"] = 4
    elif open_issues == 0 and stars >= 100:
        score -= 3
        reasons.append("issue 关闭，可能停止维护")
        signals["issues_inactive_penalty"] = -3

    age_days = _activity_age_days(candidate)
    if age_days is not None:
        signals["activity_age_days"] = age_days
        if age_days <= 14:
            score += 18
            reasons.append("两周内有提交")
            signals["activity_score"] = 18
        elif age_days <= 30:
            score += 15
            reasons.append("近期活跃")
            signals["activity_score"] = 15
        elif age_days <= 90:
            score += 10
            reasons.append("近三个月活跃")
            signals["activity_score"] = 10
        elif age_days <= 180:
            score += 5
            reasons.append("半年内有更新")
            signals["activity_score"] = 5
        elif age_days >= 365:
            score -= 10
            reasons.append("一年以上未更新")
            signals["activity_score"] = -10

    language = str(candidate.get("language", "")).lower()
    if language:
        signals["language"] = language
        if language in CHAINSTREAM_FRIENDLY_LANGUAGES:
            score += 6
            reasons.append(f"语言 {language} 与 Chainstream SDK 生态友好")
            signals["language_score"] = 6

    license_note = str(candidate.get("license_note", "")).lower()
    if license_note:
        if "mit" in license_note or "apache" in license_note or "bsd" in license_note:
            score += 10
            reasons.append("许可友好")
            signals["license_score"] = 10
        else:
            score += 3
            reasons.append("许可已知")
            signals["license_score"] = 3
    else:
        reasons.append("许可未知")
        signals["license_score"] = 0

    if not reasons:
        reasons.append("仅基于基础匹配")
    signals["total_score"] = score
    return score, "，".join(reasons), signals


def assess_chainstream_fit(candidate: dict) -> tuple[int, str, str]:
    """Backwards-compat wrapper around the default plugin's ``assess_fit``.

    Preserved as a public name so existing imports (and the 218 baseline
    tests that call ``rp.assess_chainstream_fit``) keep working byte-for-byte.
    """
    return default_data_source().assess_fit(candidate)


def infer_chainstream_targets(candidate: dict) -> dict:
    """Backwards-compat wrapper around the default plugin's ``infer_targets``."""
    return default_data_source().infer_targets(candidate)


def recommend_fork_or_build(candidate: dict) -> tuple[str, str]:
    repo_score = int(candidate.get("score", 0) or 0)
    fit_score = int(candidate.get("chainstream_fit_score", 0) or 0)
    license_note = str(candidate.get("license_note", "") or "").lower()
    license_friendly = any(token in license_note for token in ("mit", "apache", "bsd"))

    if candidate.get("is_archived"):
        return "build_new", "上游仓库已归档，无法持续接入 Chainstream，应另起新仓。"
    if fit_score >= 70 and repo_score >= 50 and license_friendly:
        return "fork_existing", "候选仓库、许可与 Chainstream 数据适配度都较强，优先 fork 验证。"
    if fit_score >= 45 and repo_score >= 25:
        return "template_clone", "候选仓库有可复用结构，但 Chainstream 数据层仍需要改造，适合 template clone。"
    if fit_score >= 35:
        return "build_new", "机会方向与 Chainstream 匹配，但现有仓库复用价值有限，建议新建。"
    return "build_new", "GitHub 候选不足以直接复用；若业务机会成立，应围绕 Chainstream 数据模型新建。"


def enrich_candidate(config: dict, candidate: dict) -> None:
    score, ranking_reason, quality_signals = score_candidate(config, candidate)
    fit_score, fit_reason, access_method = assess_chainstream_fit(candidate)
    candidate["score"] = score
    candidate["ranking_reason"] = ranking_reason
    candidate["quality_signals"] = quality_signals
    candidate["chainstream_fit_score"] = fit_score
    candidate["chainstream_fit_reason"] = fit_reason
    candidate["recommended_chainstream_access"] = access_method
    recommendation, recommendation_reason = recommend_fork_or_build(candidate)
    candidate["fork_or_build_recommendation"] = recommendation
    candidate["fork_or_build_reason"] = recommendation_reason


def candidate_sort_key(item: dict) -> tuple[int, int, int, str]:
    return (
        -int(item.get("chainstream_fit_score", 0) or 0),
        -int(item.get("score", 0) or 0),
        -int(item.get("stars", 0) or 0),
        str(item.get("name", "")),
    )


def normalize_candidates(raw_items: list[dict], query: str, config: dict) -> list[dict]:
    candidates: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        owner = owner_login(item)
        name = str(item.get("name", "")).strip()
        full_name = f"{owner}/{name}".strip("/") if owner else name
        candidate = {
            "source": "github_search",
            "name": full_name,
            "url": str(item.get("url", "")).strip(),
            "description": str(item.get("description", "") or "").strip(),
            "stars": int(item.get("stargazersCount", 0) or 0),
            "forks": int(item.get("forksCount", 0) or 0),
            "open_issues": int(item.get("openIssuesCount", 0) or 0),
            "updated_at": str(item.get("updatedAt", "") or "").strip(),
            "pushed_at": str(item.get("pushedAt", "") or "").strip(),
            "is_archived": bool(item.get("isArchived", False)),
            "is_fork": bool(item.get("isFork", False)),
            "language": str(item.get("language", "") or "").strip(),
            "default_branch": str(item.get("defaultBranch", "") or "").strip(),
            "homepage": str(item.get("homepage", "") or "").strip(),
            "fit_reason": f"Discovered from GitHub search query: {query}",
            "license_note": normalize_license(item),
        }
        enrich_candidate(config, candidate)
        candidates.append(candidate)
    candidates.sort(key=candidate_sort_key)
    return candidates


def recommend_strategy(config: dict, candidates: list[dict]) -> tuple[str, str]:
    hotspot_name = slugify(require_string(config, "meta", "hotspot_name"))
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    if not candidates:
        return "new_repo", "No viable GitHub candidates were discovered, so starting a new repo is the safest default."
    fork_or_build = str(candidates[0].get("fork_or_build_recommendation", "") or "")
    if fork_or_build == "build_new":
        return "new_repo", str(candidates[0].get("fork_or_build_reason", "")) or "Chainstream fit favors a clean new repository."
    if fork_or_build in {"fork_existing", "template_clone"}:
        return fork_or_build, str(candidates[0].get("fork_or_build_reason", "")) or "Pre-build analysis found a reusable candidate route."
    top_score = int(candidates[0].get("score", 0) or 0)
    for candidate in candidates:
        candidate_name = slugify(str(candidate.get("name", "")))
        if hotspot_name and hotspot_name in candidate_name:
            return "fork_existing", "A close name match exists on GitHub, so fork_existing is the most direct route."
    if shape in {"demo", "starter", "agent_workflow", "mcp_server"} and top_score >= 30:
        return "template_clone", "Template-friendly project shape with viable upstream repos; template_clone is the best starting point."
    return "new_repo", "Project shape favors a clean public API or controlled structure, so new_repo is the safer default."


def ensure_execution_state(config: dict) -> dict:
    return ensure_nested_dict(config, "execution_state")


def update_pre_build_analysis(config: dict, candidates: list[dict]) -> None:
    plugin = default_data_source()
    gate_field = plugin.gate_field
    analysis = ensure_nested_dict(config, "pre_build_analysis")
    fit_block = ensure_nested_dict(analysis, gate_field)
    fork_or_build = ensure_nested_dict(analysis, "fork_or_build")

    if not candidates:
        fit_block["score"] = 0
        fit_block["fit_reason"] = "No GitHub candidates were discovered; Chainstream fit remains unproven."
        fit_block["verdict"] = "hold"
        fork_or_build["recommendation"] = "build_new"
        fork_or_build["rationale"] = "No reusable repo candidate found; build_new is the default if the opportunity remains attractive."
        return

    top = candidates[0]
    fit_score = int(top.get("chainstream_fit_score", 0) or 0)
    access_method = str(top.get("recommended_chainstream_access", "") or "graphql")
    inferred = plugin.infer_targets(top)
    fit_block["score"] = min(5, max(0, round(fit_score / 20)))
    fit_block["target_capability"] = access_method
    fit_block["best_access_method"] = access_method
    fit_block["chain_groups"] = inferred["chain_groups"]
    fit_block["data_cubes"] = inferred["data_cubes"]
    fit_block["query_intent"] = inferred["query_intent"]
    fit_block["aggregation_need"] = inferred["aggregation_need"]
    fit_block["latency_need"] = inferred["latency_need"]
    fit_block["api_doc_refs"] = inferred["api_doc_refs"]
    fit_block["public_demo_safety"] = "needs_mock_data"
    fit_block["fit_reason"] = str(top.get("chainstream_fit_reason", ""))
    if fit_score >= 60:
        fit_block["verdict"] = "pass"
    elif fit_score >= 30:
        fit_block["verdict"] = "hold"
    else:
        fit_block["verdict"] = "fail"

    recommendation = str(top.get("fork_or_build_recommendation", "") or "build_new")
    fork_or_build["recommendation"] = recommendation
    fork_or_build["rationale"] = str(top.get("fork_or_build_reason", ""))
    fork_or_build["candidate_project_patterns"] = [
        {
            "pattern": require_string(config, "gate_2_project_shape", "project_shape") or "undecided",
            "why_it_fits_chainstream": str(top.get("chainstream_fit_reason", "")),
            "fork_viability": "high" if recommendation == "fork_existing" else "medium" if recommendation == "template_clone" else "low",
            "build_viability": "high" if recommendation == "build_new" else "medium",
        }
    ]
    fork_or_build["minimum_pre_build_proof"] = [
        "确认 Chainstream API docs 中存在目标 chain group 与 cube/stream",
        "写出一个可运行的 GraphQL query 或 Kafka consumption plan",
        "确认 public repo 可使用 mock/sample data，避免泄露 API key 或敏感数据",
    ]


DEFAULT_CHAINSTREAM_GRAPHQL_QUERY = """
query PipelineDataProbe {
  Solana {
    DEXTrades(
      limit: {count: 1}
      orderBy: {descending: Block_Time}
    ) {
      Block {
        Time
        Slot
      }
      Transaction {
        Hash
      }
      Trade {
        Buy {
          Currency { MintAddress }
          Amount
          PriceInUSD
        }
        Sell {
          Currency { MintAddress }
          Amount
        }
        Dex { ProtocolName }
      }
    }
  }
}
""".strip()


def chainstream_query_from_args(args: argparse.Namespace, config: dict | None = None) -> tuple[str, str]:
    return resolve_probe_query(args, config or {})


def summarize_graphql_payload(payload: dict) -> tuple[str, str]:
    if payload.get("errors"):
        errors = payload.get("errors")
        return "failed", json.dumps(errors, ensure_ascii=False)[:800]
    data = payload.get("data")
    if not isinstance(data, dict):
        return "failed", "Response did not include a data object."
    top_keys = ", ".join(data.keys())
    credits = payload.get("extensions", {}).get("credits", {}) if isinstance(payload.get("extensions"), dict) else {}
    credits_summary = f" credits={json.dumps(credits, ensure_ascii=False)[:300]}" if credits else ""
    return "passed", f"GraphQL data returned for: {top_keys or 'unknown'}{credits_summary}"


def run_chainstream_data_probe(args: argparse.Namespace, config: dict | None = None) -> dict:
    plugin = resolve_data_source(args)
    query, query_source = chainstream_query_from_args(args, config)
    result = {
        "status": "planned",
        "endpoint": args.chainstream_endpoint,
        "query_source": query_source,
        "summary": "ChainStream GraphQL probe planned; pass --execute to run it.",
        "response_keys": [],
        "credits": {},
    }
    if not args.execute:
        return result

    api_key = os.environ.get(args.chainstream_api_key_env, "").strip()
    if not api_key:
        result["status"] = "blocked"
        result["summary"] = f"Missing ChainStream API key env var: {args.chainstream_api_key_env}"
        return result

    try:
        payload = plugin.post_graphql_probe(args.chainstream_endpoint, api_key, query)
    except (PipelineError, DataSourceError) as exc:
        result["status"] = "failed"
        result["summary"] = str(exc)
        return result

    status, summary = summarize_graphql_payload(payload)
    result["status"] = status
    result["summary"] = summary
    data = payload.get("data", {})
    result["response_keys"] = list(data.keys()) if isinstance(data, dict) else []
    extensions = payload.get("extensions", {})
    if isinstance(extensions, dict) and isinstance(extensions.get("credits"), dict):
        result["credits"] = extensions["credits"]
    return result


def discover_candidates(
    config: dict,
    query: str,
    limit: int,
    sources: list[str],
    jina_query: str = "",
    x_query: str = "",
    hackernews_query: str = "",
    reddit_query: str = "",
    reddit_subreddits: list[str] | None = None,
) -> tuple[list[dict], str, str]:
    candidates: list[dict] = []
    source_records: list[dict] = []

    if "github" in sources:
        try:
            github_candidates = normalize_candidates(gh_search_repos(query, limit), query, config)
            candidates.extend(github_candidates)
            source_records.append(
                {
                    "source": "github_search",
                    "query": query,
                    "status": "searched",
                    "evidence_count": len(github_candidates),
                    "strongest_signal": github_candidates[0].get("name", "") if github_candidates else "",
                    "notes": "GitHub repository search completed.",
                }
            )
        except PipelineError as exc:
            source_records.append(
                {
                    "source": "github_search",
                    "query": query,
                    "status": "blocked",
                    "evidence_count": 0,
                    "strongest_signal": "",
                    "notes": str(exc),
                }
            )

    if "jina" in sources:
        effective_query = jina_query or query
        try:
            jina_candidates = normalize_jina_candidates(jina_search(effective_query, limit), effective_query, config)
            candidates.extend(jina_candidates)
            source_records.append(
                {
                    "source": "jina_search",
                    "query": effective_query,
                    "status": "searched",
                    "evidence_count": len(jina_candidates),
                    "strongest_signal": jina_candidates[0].get("name", "") if jina_candidates else "",
                    "notes": "Jina search completed.",
                }
            )
        except PipelineError as exc:
            source_records.append(
                {
                    "source": "jina_search",
                    "query": effective_query,
                    "status": "blocked",
                    "evidence_count": 0,
                    "strongest_signal": "",
                    "notes": str(exc),
                }
            )

    if "x" in sources:
        effective_query = x_query or query
        try:
            x_candidates = normalize_x_candidates(x_recent_search(effective_query, limit), effective_query, config)
            candidates.extend(x_candidates)
            source_records.append(
                {
                    "source": "x_search",
                    "query": effective_query,
                    "status": "searched",
                    "evidence_count": len(x_candidates),
                    "strongest_signal": x_candidates[0].get("name", "") if x_candidates else "",
                    "notes": "X recent search completed.",
                }
            )
        except PipelineError as exc:
            source_records.append(
                {
                    "source": "x_search",
                    "query": effective_query,
                    "status": "blocked",
                    "evidence_count": 0,
                    "strongest_signal": "",
                    "notes": str(exc),
                }
            )

    if "hackernews" in sources:
        effective_query = hackernews_query or query
        try:
            hn_raw = hackernews_search(effective_query, limit)
            hn_candidates = normalize_hackernews_candidates(hn_raw, effective_query, config, enrich_candidate)
            candidates.extend(hn_candidates)
            source_records.append(
                {
                    "source": "hackernews",
                    "query": effective_query,
                    "status": "searched",
                    "evidence_count": len(hn_candidates),
                    "strongest_signal": hn_candidates[0].get("name", "") if hn_candidates else "",
                    "notes": "HackerNews (Algolia) search completed.",
                }
            )
        except ExtraSourceError as exc:
            source_records.append(
                {
                    "source": "hackernews",
                    "query": effective_query,
                    "status": "blocked",
                    "evidence_count": 0,
                    "strongest_signal": "",
                    "notes": str(exc),
                }
            )

    if "reddit" in sources:
        effective_query = reddit_query or query
        try:
            reddit_raw = reddit_search(effective_query, limit, subreddits=reddit_subreddits)
            reddit_candidates = normalize_reddit_candidates(reddit_raw, effective_query, config, enrich_candidate)
            candidates.extend(reddit_candidates)
            source_records.append(
                {
                    "source": "reddit",
                    "query": effective_query,
                    "status": "searched",
                    "evidence_count": len(reddit_candidates),
                    "strongest_signal": reddit_candidates[0].get("name", "") if reddit_candidates else "",
                    "notes": f"Reddit search completed across {len(reddit_subreddits or []) or 'all'} subreddit(s).",
                }
            )
        except ExtraSourceError as exc:
            source_records.append(
                {
                    "source": "reddit",
                    "query": effective_query,
                    "status": "blocked",
                    "evidence_count": 0,
                    "strongest_signal": "",
                    "notes": str(exc),
                }
            )

    candidates, dedup_stats = dedup_candidates(candidates)
    candidates.sort(key=candidate_sort_key)
    topics_stats = enrich_candidates_with_topics(candidates, run_command, max_calls=5)
    candidates.sort(key=candidate_sort_key)
    strategy, reason = recommend_strategy(config, candidates)
    update_pre_build_analysis(config, candidates)
    gate = ensure_nested_dict(config, "gate_3_repo_routing")
    gate["discovered_query"] = query
    gate["recommended_strategy"] = strategy
    gate["recommended_reason"] = reason
    gate["candidate_repos"] = candidates
    source_context = ensure_nested_dict(config, "source_context")
    source_context["discovery_sources"] = source_records
    source_context["topics_enrichment"] = topics_stats
    source_context["dedup"] = dedup_stats
    if require_string(config, "gate_3_repo_routing", "repo_strategy") == "undecided":
        gate["repo_strategy"] = strategy
    execution_state = ensure_execution_state(config)
    discovery_state = ensure_nested_dict(execution_state, "discovery")
    discovery_state["last_run_at"] = iso_now()
    discovery_state["query"] = query
    discovery_state["discovered_count"] = len(candidates)
    if candidates:
        discovery_state["selected_candidate_name"] = str(candidates[0].get("name", ""))
        discovery_state["selected_candidate_url"] = str(candidates[0].get("url", ""))
        discovery_state["selected_candidate_index"] = 0
    else:
        discovery_state["selected_candidate_name"] = ""
        discovery_state["selected_candidate_url"] = ""
        discovery_state["selected_candidate_index"] = -1
    return candidates, strategy, reason


def prepare_new_repo_workspace(config: dict, workspace: Path, *, reuse_existing: bool = False) -> None:
    if reuse_existing and workspace.exists() and any(workspace.iterdir()):
        readme = workspace / "README.md"
        notes = workspace / "PIPELINE_NOTES.md"
        if not readme.exists():
            hotspot_name = require_string(config, "meta", "hotspot_name")
            thesis = require_string(config, "decision", "one_line_thesis")
            readme.write_text(
                f"# {hotspot_name}\n\n{thesis or 'Generated by Hotspot To GitHub Pipeline.'}\n",
                encoding="utf-8",
            )
        if not notes.exists():
            notes.write_text(
                "# Pipeline Notes\n\n"
                f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}\n"
                f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}\n"
                f"- Project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}\n",
                encoding="utf-8",
            )
        return
    ensure_empty_workspace(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    hotspot_name = require_string(config, "meta", "hotspot_name")
    thesis = require_string(config, "decision", "one_line_thesis")
    readme = f"# {hotspot_name}\n\n{thesis or 'Generated by Hotspot To GitHub Pipeline.'}\n"
    notes = (
        "# Pipeline Notes\n\n"
        f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}\n"
        f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}\n"
        f"- Project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}\n"
    )
    (workspace / "README.md").write_text(readme, encoding="utf-8")
    (workspace / "PIPELINE_NOTES.md").write_text(notes, encoding="utf-8")


def prepare_cloned_workspace(workspace: Path, url: str, *, reuse_existing: bool = False) -> None:
    if reuse_existing and workspace.exists() and (workspace / ".git").exists():
        return
    ensure_empty_workspace(workspace)
    result = run_command(["git", "clone", url, str(workspace)])
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "git clone failed")


def prepare_workspace(config: dict, workspace: Path, candidate: dict, execute: bool, *, reuse_existing: bool = False) -> list[str]:
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    actions: list[str] = []
    if strategy == "new_repo":
        if reuse_existing and workspace.exists() and any(workspace.iterdir()):
            actions.append(f"reuse existing workspace {workspace}")
            actions.append("ensure README.md and PIPELINE_NOTES.md (no overwrite)")
        else:
            actions.append(f"mkdir {workspace}")
            actions.append("seed README.md and PIPELINE_NOTES.md")
        if execute:
            prepare_new_repo_workspace(config, workspace, reuse_existing=reuse_existing)
    elif strategy in {"fork_existing", "template_clone"}:
        url = candidate.get("url", "")
        if not url:
            raise PipelineError(f"{strategy} requires candidate_repos[0].url")
        if reuse_existing and workspace.exists() and (workspace / ".git").exists():
            actions.append(f"reuse existing cloned workspace {workspace}")
        else:
            actions.append(f"git clone {url} {workspace}")
        if execute:
            prepare_cloned_workspace(workspace, url, reuse_existing=reuse_existing)
    else:
        raise PipelineError(f"Unsupported or undecided repo strategy: {strategy}")
    return actions


def run_shell_command(command: str, cwd: Path, execute: bool) -> dict:
    if not command:
        return {"status": "skipped", "stdout": "", "stderr": "", "command": ""}
    if not execute:
        return {"status": "planned", "stdout": "", "stderr": "", "command": command}
    result = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, check=False)
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": command,
    }


def run_probe(config: dict, workspace: Path, execute: bool) -> dict:
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
    results = {
        "install": run_shell_command(str(commands.get("install", "") or ""), workspace, execute),
        "build": run_shell_command(str(commands.get("build", "") or ""), workspace, execute),
        "test": run_shell_command(str(commands.get("test", "") or ""), workspace, execute),
    }
    failures = [step for step, result in results.items() if result["status"] == "failed"]
    if failures:
        failure_details = []
        for step in failures:
            entry = results[step]
            failure_details.append(
                f"Command failed in {workspace}:\n$ {entry['command']}\n\nstdout:\n{entry['stdout']}\n\nstderr:\n{entry['stderr']}"
            )
        raise PipelineError("\n\n".join(failure_details))
    return results


def ensure_git_user_configured() -> None:
    name = run_command(["git", "config", "--get", "user.name"])
    email = run_command(["git", "config", "--get", "user.email"])
    if name.returncode != 0 or email.returncode != 0 or not name.stdout.strip() or not email.stdout.strip():
        raise PipelineError("Git user.name and user.email must be configured before publish.")


def remove_existing_git_dir(workspace: Path) -> None:
    git_dir = workspace / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)


def initialize_git_repo(workspace: Path, branch: str) -> None:
    ensure_git_user_configured()
    remove_existing_git_dir(workspace)
    result = run_command(["git", "init", "-b", branch], cwd=workspace)
    if result.returncode != 0:
        fallback = run_command(["git", "init"], cwd=workspace)
        if fallback.returncode != 0:
            raise PipelineError(fallback.stderr.strip() or "git init failed")
        checkout = run_command(["git", "checkout", "-b", branch], cwd=workspace)
        if checkout.returncode != 0:
            raise PipelineError(checkout.stderr.strip() or "git checkout -b failed")
    add_result = run_command(["git", "add", "."], cwd=workspace)
    if add_result.returncode != 0:
        raise PipelineError(add_result.stderr.strip() or "git add failed")
    status = run_command(["git", "status", "--porcelain"], cwd=workspace)
    if status.returncode != 0:
        raise PipelineError(status.stderr.strip() or "git status failed")
    if status.stdout.strip():
        commit = run_command(["git", "commit", "-m", "Initialize hotspot pipeline workspace"], cwd=workspace)
        if commit.returncode != 0:
            raise PipelineError(commit.stderr.strip() or "git commit failed")


def current_gh_login() -> str:
    result = run_command(["gh", "api", "user", "--jq", ".login"])
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "Unable to resolve current GitHub user.")
    return result.stdout.strip()


def publish_workspace(config: dict, workspace: Path, candidate: dict, execute: bool, allow_publish: bool) -> tuple[list[str], str]:
    if not execute:
        owner = repo_owner(config) or "<github-owner>"
        return ["dry-run publish plan generated"], f"{owner}/{repo_name(config)}"
    if not allow_publish:
        raise PipelineError("Publishing requires --allow-publish together with --execute.")

    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    actions: list[str] = []
    if strategy == "fork_existing":
        url = candidate.get("url", "")
        if not url:
            raise PipelineError("fork_existing requires candidate_repos[0].url")
        result = run_command(["gh", "repo", "fork", url, "--clone=false", "--remote=false"])
        if result.returncode != 0:
            raise PipelineError(result.stderr.strip() or "gh repo fork failed")
        actions.append("gh repo fork executed")
        repo_ref = str(candidate.get("name", ""))
        return actions, repo_ref

    owner = repo_owner(config) or current_gh_login()
    name = repo_name(config)
    visibility = repo_visibility(config)
    branch = default_branch(config)
    initialize_git_repo(workspace, branch)
    repo_ref = f"{owner}/{name}"
    command = ["gh", "repo", "create", repo_ref, f"--{visibility}", "--source", str(workspace), "--push"]
    result = run_command(command)
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "gh repo create failed")
    actions.append(f"gh repo create {repo_ref} executed")
    return actions, repo_ref


def command_status(result: dict) -> str:
    status = str(result.get("status", "not_run"))
    if status == "planned":
        return "not_run"
    return status


def derive_gate4_verdict(results: dict) -> str:
    statuses = {command_status(item) for item in results.values()}
    if "failed" in statuses:
        return "fail"
    if "passed" in statuses and statuses.issubset({"passed", "skipped"}):
        return "pass"
    return "hold"


def replace_line(text: str, prefix: str, new_line: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = new_line
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def replace_enumerated_line(text: str, index: int, new_line: str) -> str:
    marker = f"{index}."
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        if line.startswith(marker):
            lines[line_index] = new_line
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def replace_section_numbered_items(text: str, header: str, items: list[str], count: int = 3) -> str:
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == header:
            start = idx + 1
            break
    if start is None:
        return text
    numbered_seen = 0
    for idx in range(start, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("## ") and idx > start:
            break
        for number in range(1, count + 1):
            if stripped.startswith(f"{number}."):
                value = items[number - 1] if number - 1 < len(items) else ""
                lines[idx] = f"{number}. {value}" if value else f"{number}."
                numbered_seen += 1
                break
        if numbered_seen >= count:
            break
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def update_probe_run_file(case_dir: Path, config: dict, workspace: Path, candidate: dict, results: dict, mode: str) -> None:
    path = probe_run_file(case_dir)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    text = replace_line(text, "- Hotspot ID:", f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}")
    text = replace_line(text, "- Hotspot:", f"- Hotspot: {require_string(config, 'meta', 'hotspot_name')}")
    text = replace_line(text, "- Date:", f"- Date: {require_string(config, 'meta', 'date')}")
    text = replace_line(text, "- Owner:", f"- Owner: {require_string(config, 'meta', 'owner')}")
    text = replace_line(text, "- Source gate:", f"- Source gate: {require_string(config, 'build_probe', 'source_gate')}")
    text = replace_line(text, "- Repo strategy:", f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}")
    text = replace_line(text, "- Candidate repo or template:", f"- Candidate repo or template: {candidate.get('name', '') or candidate.get('url', '')}")
    text = replace_line(text, "- Hypothesis:", f"- Hypothesis: {require_string(config, 'build_probe', 'hypothesis')}")
    text = replace_line(text, "- Experiment type:", f"- Experiment type: {require_string(config, 'build_probe', 'experiment_type')}")
    text = replace_line(text, "- Timebox:", f"- Timebox: {require_string(config, 'build_probe', 'timebox')}")
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
    clone_command = ""
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    if strategy in {"fork_existing", "template_clone"} and candidate.get("url"):
        clone_command = f"git clone {candidate.get('url')} {workspace}"
    elif strategy == "new_repo":
        clone_command = f"mkdir {workspace}"
    text = replace_line(text, "- Clone command:", f"- Clone command: {clone_command}")
    text = replace_line(text, "- Install command:", f"- Install command: {commands.get('install', '')}")
    text = replace_line(text, "- Build command:", f"- Build command: {commands.get('build', '')}")
    text = replace_line(text, "- Test command:", f"- Test command: {commands.get('test', '')}")
    success = config.get("build_probe", {}).get("success_signal", [])
    failure = config.get("build_probe", {}).get("failure_signal", [])
    text = replace_line(text, "- Success signal:", f"- Success signal: {success[0] if isinstance(success, list) and success else ''}")
    text = replace_line(text, "- Failure signal:", f"- Failure signal: {failure[0] if isinstance(failure, list) and failure else ''}")
    kill_signals = config.get("gate_4_buildability", {}).get("kill_signals", [])
    text = replace_line(text, "- Kill signal to watch:", f"- Kill signal to watch: {kill_signals[0] if isinstance(kill_signals, list) and kill_signals else ''}")
    text = replace_enumerated_line(text, 1, f"1. Mode: {mode}")
    text = replace_enumerated_line(text, 2, f"2. Workspace: {workspace}")
    text = replace_enumerated_line(text, 3, f"3. Strategy: {strategy}")
    text = replace_line(text, "- Observation 1:", f"- Observation 1: install={command_status(results.get('install', {}))}")
    text = replace_line(text, "- Observation 2:", f"- Observation 2: build={command_status(results.get('build', {}))}")
    text = replace_line(text, "- Observation 3:", f"- Observation 3: test={command_status(results.get('test', {}))}")
    statuses = [command_status(results.get(name, {})) for name in ("install", "build", "test")]
    overall = "strong_pass" if "failed" not in statuses and "passed" in statuses else "mixed"
    if "failed" in statuses:
        overall = "fail"
    text = replace_line(text, "- Result:", f"- Result: {overall}")
    text = replace_line(text, "- Build status:", f"- Build status: {command_status(results.get('build', {}))}")
    text = replace_line(text, "- Test status:", f"- Test status: {command_status(results.get('test', {}))}")
    text = replace_line(text, "- Did we trigger a kill signal:", "- Did we trigger a kill signal: no")
    text = replace_line(text, "- What changed in the pipeline view:", f"- What changed in the pipeline view: buildability verdict now leans {derive_gate4_verdict(results)}")
    next_status = "publish" if mode == "publish" else require_string(config, "decision", "final_status")
    text = replace_line(text, "- Recommended next status:", f"- Recommended next status: {next_status}")
    text = replace_line(text, "- Biggest surprise:", f"- Biggest surprise: selected workspace route was {strategy}")
    text = replace_line(text, "- What we learned:", f"- What we learned: install/build/test statuses are {statuses}")
    text = replace_line(text, "- What remains unresolved:", f"- What remains unresolved: {require_string(config, 'decision', 'primary_constraint')}")
    path.write_text(text, encoding="utf-8")


def update_memo_file(case_dir: Path, config: dict, candidate: dict, results: dict | None = None, mode: str = "inspect") -> None:
    path = case_dir / "03-publish-decision-memo.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    text = replace_line(text, "`Decision`:", f"`Decision`: `{require_string(config, 'decision', 'final_status')}`")
    thesis = require_string(config, "decision", "one_line_thesis")
    if thesis:
        text = replace_line(text, "`One-line thesis`:", f"`One-line thesis`: {thesis}")
    text = replace_line(text, "`Primary constraint`:", f"`Primary constraint`: `{require_string(config, 'decision', 'primary_constraint') or 'unknown'}`")
    text = replace_line(text, "- Hotspot ID:", f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}")
    text = replace_line(text, "- Hotspot:", f"- Hotspot: {require_string(config, 'meta', 'hotspot_name')}")
    text = replace_line(text, "- Original question:", f"- Original question: {require_string(config, 'hotspot_question', 'original_question')}")
    text = replace_line(text, "- Reframed question:", f"- Reframed question: {require_string(config, 'hotspot_question', 'reframed_question')}")
    text = replace_line(text, "- Why repo:", f"- Why repo: {require_string(config, 'hotspot_question', 'why_this_should_be_a_repo')}")
    text = replace_line(text, "- Falsified if:", f"- Falsified if: {', '.join(config.get('hotspot_question', {}).get('falsified_if', []))}")
    chainstream = config.get("pre_build_analysis", {}).get("chainstream_fit", {})
    fork_build = config.get("pre_build_analysis", {}).get("fork_or_build", {})
    if not isinstance(chainstream, dict):
        chainstream = {}
    if not isinstance(fork_build, dict):
        fork_build = {}
    text = replace_line(text, "- Best Chainstream access method:", f"- Best Chainstream access method: {chainstream.get('best_access_method', '')}")
    chain_groups = chainstream.get("chain_groups", [])
    text = replace_line(text, "- Target chain groups:", f"- Target chain groups: {', '.join(chain_groups) if isinstance(chain_groups, list) else chain_groups}")
    cubes = chainstream.get("data_cubes", [])
    text = replace_line(text, "- Candidate data cubes / streams:", f"- Candidate data cubes / streams: {', '.join(cubes) if isinstance(cubes, list) else cubes}")
    text = replace_line(text, "- Query or stream intent:", f"- Query or stream intent: {chainstream.get('query_intent', '')}")
    refs = chainstream.get("api_doc_refs", [])
    text = replace_line(text, "- API doc references:", f"- API doc references: {', '.join(refs) if isinstance(refs, list) else refs}")
    graphql_probe = chainstream.get("graphql_probe", {})
    if not isinstance(graphql_probe, dict):
        graphql_probe = {}
    text = replace_line(text, "- GraphQL probe status:", f"- GraphQL probe status: {graphql_probe.get('status', '')}")
    text = replace_line(text, "- GraphQL probe summary:", f"- GraphQL probe summary: {graphql_probe.get('summary', '')}")
    text = replace_line(text, "- Public demo data safety:", f"- Public demo data safety: {chainstream.get('public_demo_safety', '')}")
    text = replace_line(text, "- Fork or build recommendation:", f"- Fork or build recommendation: {fork_build.get('recommendation', '')}")
    text = replace_line(text, "- Why:", f"- Why: {fork_build.get('rationale', '')}")
    text = replace_line(text, "- Project shape:", f"- Project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}")
    text = replace_line(text, "- Repo strategy:", f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}")
    text = replace_line(text, "- Candidate repo or template:", f"- Candidate repo or template: {candidate.get('name', '') or candidate.get('url', '')}")
    text = replace_line(text, "- Why this route:", f"- Why this route: {require_string(config, 'gate_3_repo_routing', 'recommended_reason')}")
    text = replace_line(text, "`Veto from gate`:", f"`Veto from gate`: `{require_string(config, 'decision', 'veto_from_gate') or 'none'}`")
    case_display = case_dir.as_posix()
    text = replace_line(text, "`Data probe command`:", f"`Data probe command`: python3 run_pipeline.py --case-dir \"{case_display}\" --mode data-probe --execute")
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
    text = replace_line(text, "`Build command`:", f"`Build command`: {commands.get('build', '')}")
    text = replace_line(text, "`Test command`:", f"`Test command`: {commands.get('test', '')}")
    text = replace_line(text, "`Timebox`:", f"`Timebox`: {require_string(config, 'build_probe', 'timebox')}")
    text = replace_line(text, "`Owner`:", f"`Owner`: {require_string(config, 'meta', 'owner')}")
    success = config.get("build_probe", {}).get("success_signal", [])
    failure = config.get("build_probe", {}).get("failure_signal", [])
    text = replace_line(text, "`Success signal`:", f"`Success signal`: {success[0] if isinstance(success, list) and success else ''}")
    text = replace_line(text, "`Failure signal`:", f"`Failure signal`: {failure[0] if isinstance(failure, list) and failure else ''}")
    text = replace_line(text, "`Next review date`:", f"`Next review date`: {require_string(config, 'decision', 'next_review_date')}")
    text = replace_line(text, "`Previous status`:", f"`Previous status`: {config.get('review_log', [{}])[-1].get('previous_status', require_string(config, 'decision', 'final_status'))}")
    last_review = config.get("review_log", [{}])[-1] if isinstance(config.get("review_log"), list) and config.get("review_log") else {}
    text = replace_line(text, "`What changed since last round`:", f"`What changed since last round`: {last_review.get('what_changed', '')}")
    text = replace_line(text, "`What remains unresolved`:", f"`What remains unresolved`: {require_string(config, 'decision', 'primary_constraint')}")
    text = replace_line(text, "`Lesson so far`:", f"`Lesson so far`: {last_review.get('lessons', '')}")
    strongest = []
    for gate_key in ("gate_1_hotspot_signal", "gate_2_project_shape", "gate_3_repo_routing"):
        evidence = config.get(gate_key, {}).get("evidence", [])
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict) and item.get("summary"):
                    strongest.append(str(item["summary"]))
    if candidate.get("ranking_reason"):
        strongest.append(f"Top candidate: {candidate.get('name', '')} ({candidate.get('ranking_reason', '')})")
    if candidate.get("chainstream_fit_reason"):
        strongest.append(f"Chainstream fit: {candidate.get('chainstream_fit_reason', '')}")
    text = replace_section_numbered_items(text, "## 6. Strongest Evidence", strongest)
    risks = []
    for gate_key in ("gate_3_repo_routing", "gate_4_buildability", "gate_5_publish_decision"):
        signals = config.get(gate_key, {}).get("kill_signals", [])
        if isinstance(signals, list):
            risks.extend(str(item) for item in signals if item)
    text = replace_section_numbered_items(text, "## 7. Biggest Risks", risks)
    path.write_text(text, encoding="utf-8")


def update_review_checkpoint_file(case_dir: Path, config: dict, candidate: dict) -> None:
    path = case_dir / "05-review-checkpoint.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    review_log = config.get("review_log", [])
    latest = review_log[-1] if isinstance(review_log, list) and review_log else {}
    text = replace_line(text, "- Hotspot ID:", f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}")
    text = replace_line(text, "- Hotspot:", f"- Hotspot: {require_string(config, 'meta', 'hotspot_name')}")
    text = replace_line(text, "- Review date:", f"- Review date: {latest.get('date', '')}")
    text = replace_line(text, "- Reviewer:", f"- Reviewer: {require_string(config, 'meta', 'owner')}")
    text = replace_line(text, "- Previous status:", f"- Previous status: {latest.get('previous_status', '')}")
    text = replace_line(text, "- New status:", f"- New status: {latest.get('new_status', '')}")
    text = replace_line(text, "- New evidence:", f"- New evidence: {candidate.get('name', '') or require_string(config, 'execution_state', 'probe', 'summary')}")
    text = replace_line(text, "- New Chainstream capability insight:", f"- New Chainstream capability insight: {candidate.get('chainstream_fit_reason', '')}")
    text = replace_line(text, "- Invalidated assumption:", f"- Invalidated assumption: {require_string(config, 'decision', 'primary_constraint')}")
    text = replace_line(text, "- New repo candidate:", f"- New repo candidate: {candidate.get('name', '') or candidate.get('url', '')}")
    text = replace_line(text, "- New risk:", f"- New risk: {require_string(config, 'decision', 'veto_from_gate')}")
    text = replace_line(text, "- Updated Chainstream access method:", f"- Updated Chainstream access method: {require_string(config, 'pre_build_analysis', 'chainstream_fit', 'best_access_method')}")
    text = replace_line(text, "- Updated Chainstream data-probe status:", f"- Updated Chainstream data-probe status: {require_string(config, 'execution_state', 'data_probe', 'status')}")
    text = replace_line(text, "- Updated fork/build recommendation:", f"- Updated fork/build recommendation: {require_string(config, 'pre_build_analysis', 'fork_or_build', 'recommendation')}")
    text = replace_line(text, "- Updated project shape:", f"- Updated project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}")
    text = replace_line(text, "- Updated repo strategy:", f"- Updated repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}")
    text = replace_line(text, "- Continue / pause / stop:", f"- Continue / pause / stop: {require_string(config, 'decision', 'final_status')}")
    text = replace_line(text, "- Next action:", f"- Next action: {require_string(config, 'decision', 'next_action')}")
    text = replace_line(text, "- Next review date:", f"- Next review date: {require_string(config, 'decision', 'next_review_date')}")
    text = replace_line(text, "- What we got right:", f"- What we got right: {latest.get('what_changed', '')}")
    text = replace_line(text, "- What we got wrong:", f"- What we got wrong: {require_string(config, 'decision', 'veto_from_gate')}")
    text = replace_line(text, "- What we still do not know:", f"- What we still do not know: {require_string(config, 'decision', 'primary_constraint')}")
    path.write_text(text, encoding="utf-8")


def detect_kill_signal_triggers(config: dict, results: dict) -> list[str]:
    kill_signals = config.get("gate_4_buildability", {}).get("kill_signals", [])
    if not isinstance(kill_signals, list):
        return []
    triggered: list[str] = []
    haystacks = []
    for entry in results.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "failed":
            continue
        haystacks.append(str(entry.get("stderr", "")).lower())
        haystacks.append(str(entry.get("stdout", "")).lower())
    for raw_signal in kill_signals:
        signal = str(raw_signal).strip()
        if not signal:
            continue
        token = signal.lower()
        for hay in haystacks:
            if token and token in hay:
                triggered.append(signal)
                break
    return dedupe_terms(triggered)


def evaluate_publish_readiness(config: dict) -> str:
    execution_state = ensure_nested_dict(config, "execution_state")
    data_probe_status = require_string(config, "execution_state", "data_probe", "status")
    probe_install = require_string(config, "execution_state", "probe", "install_status")
    probe_build = require_string(config, "execution_state", "probe", "build_status")
    probe_test = require_string(config, "execution_state", "probe", "test_status")
    fit_verdict = require_string(config, "pre_build_analysis", "chainstream_fit", "verdict")
    gate4_verdict = require_string(config, "gate_4_buildability", "verdict")
    publish_status = require_string(config, "execution_state", "publish", "publish_status")
    target_capability = require_string(config, "pre_build_analysis", "chainstream_fit", "target_capability")
    kafka_probe_status = require_string(config, "execution_state", "kafka_probe", "status")

    probe_statuses = {probe_install, probe_build, probe_test} - {""}
    probe_pass = bool(probe_statuses) and probe_statuses.issubset({"passed", "skipped"})
    data_probe_pass = data_probe_status == "passed"
    fit_pass = fit_verdict == "pass"
    kafka_required = target_capability == "kafka"
    kafka_pass = (kafka_probe_status == "passed") if kafka_required else True

    if publish_status == "passed":
        readiness = "published"
        reason = "publish 已经成功执行。"
    elif probe_pass and data_probe_pass and fit_pass and gate4_verdict == "pass" and kafka_pass:
        readiness = "ready"
        reason = "data-probe / build-probe 均通过，且 Chainstream fit 判定为 pass。"
        if kafka_required:
            reason += " Kafka probe 也已通过。"
    elif "failed" in probe_statuses or gate4_verdict == "fail":
        readiness = "blocked_buildability"
        reason = "build-probe 出现失败，需要先解决可构建性。"
    elif kafka_required and kafka_probe_status in {"failed", "blocked"}:
        readiness = "blocked_kafka_probe"
        reason = "Chainstream Kafka probe 未通过；目标 capability=kafka 必须先解决。"
    elif data_probe_status in {"failed", "blocked"}:
        readiness = "blocked_data_probe"
        reason = "ChainStream data-probe 未通过，需先解决数据访问。"
    elif not data_probe_status and not probe_statuses:
        readiness = "not_started"
        reason = "尚未执行任何 probe。"
    else:
        readiness = "in_progress"
        reason = "部分 probe 已完成，但尚未集齐发布前条件。"

    state = ensure_nested_dict(execution_state, "publish_readiness")
    state["status"] = readiness
    state["reason"] = reason
    state["last_evaluated_at"] = iso_now()
    return readiness


def update_gate_after_probe(config: dict, workspace: Path, candidate: dict, results: dict, mode: str, repo_ref: str = "") -> None:
    execution_state = ensure_execution_state(config)
    probe_state = ensure_nested_dict(execution_state, "probe")
    probe_state["last_run_at"] = iso_now()
    probe_state["workspace"] = str(workspace)
    probe_state["install_status"] = command_status(results.get("install", {}))
    probe_state["build_status"] = command_status(results.get("build", {}))
    probe_state["test_status"] = command_status(results.get("test", {}))
    probe_state["summary"] = (
        f"candidate={candidate.get('name', '') or 'none'} "
        f"install={probe_state['install_status']} build={probe_state['build_status']} test={probe_state['test_status']}"
    )
    gate4 = ensure_nested_dict(config, "gate_4_buildability")
    gate4["verdict"] = derive_gate4_verdict(results)
    if gate4["verdict"] == "pass" and int(gate4.get("score", 0) or 0) == 0:
        gate4["score"] = 4
    triggered = detect_kill_signal_triggers(config, results)
    if triggered:
        gate4["kill_signals_triggered"] = triggered
    decision = ensure_nested_dict(config, "decision")
    if mode == "probe" and gate4["verdict"] == "pass" and decision.get("final_status") in {"draft", "watch"}:
        decision["final_status"] = "probe"
        decision["summary"] = "Probe completed successfully; ready for publish decision."
        decision["next_action"] = "review publish readiness and decide whether to publish"
    if gate4["verdict"] == "fail":
        decision["primary_constraint"] = "buildability"
        decision["veto_from_gate"] = "gate_4_buildability"
        if triggered:
            decision["next_action"] = f"address triggered kill signal: {triggered[0]}"
        else:
            decision["next_action"] = "fix install/build/test failure before retrying probe"
    publish_state = ensure_nested_dict(execution_state, "publish")
    if mode == "publish":
        publish_state["last_run_at"] = iso_now()
        publish_state["repo_ref"] = repo_ref
        publish_state["publish_status"] = "passed"
        publish_state["summary"] = f"Published via {require_string(config, 'gate_3_repo_routing', 'repo_strategy')} to {repo_ref}"
        gate5 = ensure_nested_dict(config, "gate_5_publish_decision")
        gate5["verdict"] = "pass"
        if int(gate5.get("score", 0) or 0) == 0:
            gate5["score"] = 4
        decision["final_status"] = "publish"
        decision["summary"] = f"Published {repo_ref} after successful probe."
        decision["next_action"] = "monitor repository adoption"

    readiness = evaluate_publish_readiness(config)
    if mode == "probe" and readiness == "ready" and decision.get("final_status") not in {"publish", "publish_ready"}:
        decision["final_status"] = "publish_ready"
        decision["summary"] = "All pre-publish probes passed; awaiting human publish decision."
        decision["next_action"] = "run --mode publish --execute --allow-publish to publish"
    decision["next_review_date"] = compute_next_review_date(gate4["verdict"])


def append_review_log(config: dict, previous_status: str, new_status: str, what_changed: str, lessons: str) -> None:
    review_log = config.get("review_log")
    if not isinstance(review_log, list):
        review_log = []
        config["review_log"] = review_log
    review_log.append(
        {
            "date": iso_now(),
            "previous_status": previous_status,
            "new_status": new_status,
            "what_changed": what_changed,
            "lessons": lessons,
        }
    )


def update_pool_row(pool_file: Path, config: dict, case_dir: Path) -> None:
    hotspot_id = require_string(config, "meta", "hotspot_id")
    hotspot_name = require_string(config, "meta", "hotspot_name")
    owner = require_string(config, "meta", "owner")
    status = require_string(config, "decision", "final_status")
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    last_review = (
        require_string(config, "execution_state", "publish", "last_run_at")
        or require_string(config, "execution_state", "probe", "last_run_at")
        or require_string(config, "execution_state", "discovery", "last_run_at")
        or require_string(config, "meta", "date")
    )
    next_review = require_string(config, "decision", "next_review_date")
    thesis = require_string(config, "decision", "one_line_thesis")
    try:
        case_display = case_dir.relative_to(ROOT).as_posix()
    except ValueError:
        case_display = str(case_dir)
    if "T" in last_review:
        last_review = last_review.split("T", 1)[0]
    new_row = (
        f"| {hotspot_id} | {hotspot_name} | {owner} | {status} | {shape} | "
        f"{strategy} | {last_review} | {next_review} | `{case_display}` | {thesis} |"
    )
    if pool_file.exists():
        lines = pool_file.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# Pipeline Pool",
            "",
            "| ID | Hotspot | Owner | Status | Project Shape | Repo Strategy | Last Review | Next Review | Case Folder | One-line Note |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(f"| {hotspot_id} |"):
            lines[index] = new_row
            updated = True
            break
    if not updated:
        lines.append(new_row)
    pool_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def writeback_discovery(gate_file: Path, config: dict, case_dir: Path, pool_file: Path) -> None:
    previous_status = require_string(config, "decision", "final_status")
    append_review_log(
        config,
        previous_status=previous_status,
        new_status=previous_status,
        what_changed=f"discovered {config.get('execution_state', {}).get('discovery', {}).get('discovered_count', 0)} candidate repos and updated pre-build Chainstream analysis",
        lessons=require_string(config, "gate_3_repo_routing", "recommended_reason"),
    )
    decision = ensure_nested_dict(config, "decision")
    if not decision.get("next_review_date"):
        decision["next_review_date"] = compute_next_review_date("planned")
    evaluate_publish_readiness(config)
    dump_gate_file(gate_file, config)
    update_pool_row(pool_file, config, case_dir)
    candidate = candidate_repo(config, 0) if config.get("gate_3_repo_routing", {}).get("candidate_repos") else {}
    update_memo_file(case_dir, config, candidate, mode="discover")
    update_review_checkpoint_file(case_dir, config, candidate)


def update_gate_after_data_probe(config: dict, result: dict) -> None:
    execution_state = ensure_execution_state(config)
    data_probe_state = ensure_nested_dict(execution_state, "data_probe")
    data_probe_state["last_run_at"] = iso_now()
    data_probe_state["status"] = str(result.get("status", "not_run"))
    data_probe_state["endpoint"] = str(result.get("endpoint", ""))
    data_probe_state["query_source"] = str(result.get("query_source", ""))
    data_probe_state["summary"] = str(result.get("summary", ""))
    data_probe_state["response_keys"] = result.get("response_keys", [])
    data_probe_state["credits"] = result.get("credits", {})

    plugin = default_data_source()
    gate_field = plugin.gate_field
    fit_block = ensure_nested_dict(config, "pre_build_analysis", gate_field)
    fit_block["graphql_probe"] = {
        "last_run_at": data_probe_state["last_run_at"],
        "status": data_probe_state["status"],
        "endpoint": data_probe_state["endpoint"],
        "query_source": data_probe_state["query_source"],
        "summary": data_probe_state["summary"],
        "response_keys": data_probe_state["response_keys"],
        "credits": data_probe_state["credits"],
    }
    decision = ensure_nested_dict(config, "decision")
    status = str(result.get("status", ""))
    veto_path = f"pre_build_analysis.{gate_field}"
    if status == "passed":
        fit_block["verdict"] = "pass"
        if int(fit_block.get("score", 0) or 0) < 4:
            fit_block["score"] = 4
        if decision.get("final_status") in {"draft", "watch"}:
            decision["final_status"] = "probe"
            decision["primary_constraint"] = "buildability"
            decision["next_action"] = "run build probe after ChainStream GraphQL data probe passed"
        if decision.get("veto_from_gate") == veto_path:
            decision["veto_from_gate"] = ""
    elif status in {"blocked", "failed"}:
        fit_block["verdict"] = "hold" if status == "blocked" else "fail"
        decision["primary_constraint"] = gate_field
        decision["veto_from_gate"] = veto_path
        if status == "failed":
            decision["next_action"] = "investigate ChainStream GraphQL endpoint or query before retrying"
        elif status == "blocked":
            decision["next_action"] = "configure CHAINSTREAM_API_KEY then re-run data-probe"
    decision["next_review_date"] = compute_next_review_date(status)
    evaluate_publish_readiness(config)


def writeback_data_probe(gate_file: Path, config: dict, case_dir: Path, pool_file: Path, result: dict) -> None:
    previous_status = require_string(config, "decision", "final_status")
    update_gate_after_data_probe(config, result)
    new_status = require_string(config, "decision", "final_status")
    append_review_log(
        config,
        previous_status=previous_status,
        new_status=new_status,
        what_changed=f"ChainStream GraphQL data probe {result.get('status', 'not_run')}",
        lessons=str(result.get("summary", "")),
    )
    dump_gate_file(gate_file, config)
    update_pool_row(pool_file, config, case_dir)
    candidate = candidate_repo(config, 0) if config.get("gate_3_repo_routing", {}).get("candidate_repos") else {}
    update_memo_file(case_dir, config, candidate, mode="data-probe")
    update_review_checkpoint_file(case_dir, config, candidate)


def writeback_probe(gate_file: Path, config: dict, case_dir: Path, pool_file: Path, workspace: Path, candidate: dict, results: dict, mode: str, repo_ref: str = "") -> None:
    previous_status = require_string(config, "decision", "final_status")
    update_gate_after_probe(config, workspace, candidate, results, mode, repo_ref)
    new_status = require_string(config, "decision", "final_status")
    append_review_log(
        config,
        previous_status=previous_status,
        new_status=new_status,
        what_changed=f"{mode} executed in workspace {workspace}",
        lessons=f"install/build/test -> {command_status(results.get('install', {}))}/{command_status(results.get('build', {}))}/{command_status(results.get('test', {}))}",
    )
    dump_gate_file(gate_file, config)
    update_probe_run_file(case_dir, config, workspace, candidate, results, mode)
    update_pool_row(pool_file, config, case_dir)
    update_memo_file(case_dir, config, candidate, results, mode)
    update_review_checkpoint_file(case_dir, config, candidate)


def _run_probe_or_publish_branch(
    args: argparse.Namespace,
    config: dict,
    gate_file: Path,
    case_dir: Path,
    pool_file: Path,
    workspace_root: Path,
) -> int:
    workspace = workspace_dir(config, workspace_root)
    candidate = candidate_repo(config, args.candidate_index)

    print_section("Workspace Actions")
    actions = prepare_workspace(
        config,
        workspace,
        candidate,
        args.execute,
        reuse_existing=getattr(args, "reuse_existing_workspace", False),
    )
    for action in actions:
        print(action)

    if getattr(args, "auto_infer_build_commands", False):
        print_section("Build Command Inference")
        inference = auto_fill_build_commands(
            config,
            candidate,
            workspace if workspace.exists() else None,
            only_if_empty=not getattr(args, "auto_infer_overwrite", False),
        )
        threshold = int(getattr(args, "auto_infer_confidence_threshold", 40))
        if inference["confidence"] < threshold:
            print(f"  inference confidence={inference['confidence']} below threshold={threshold}; build_commands left untouched")
        else:
            for key, value in inference.get("applied", {}).items():
                print(f"  applied {key}={value!r}")
            for key, value in inference.get("skipped", {}).items():
                print(f"  skipped {key} (existing={value!r})")
            for line in inference.get("evidence", []):
                print(f"  evidence: {line}")

    print_section("Probe Actions")
    probe_results = run_probe(config, workspace, args.execute)
    for step, result in probe_results.items():
        print(f"{step}: {command_status(result)}")

    repo_ref = ""
    if args.mode == "publish":
        print_section("Publish Actions")
        publish_actions, repo_ref = publish_workspace(config, workspace, candidate, args.execute, args.allow_publish)
        for action in publish_actions:
            print(action)
        if args.execute and args.allow_publish:
            owner = repo_owner(config) or current_gh_login()
            language = str(config.get("repo_meta", {}).get("language", "")) or str(candidate.get("language", ""))
            post_result = apply_post_publish_templates(
                workspace,
                config,
                repo_name=repo_name(config),
                github_owner=owner,
                language=language,
            )
            publish_state = ensure_nested_dict(ensure_execution_state(config), "publish")
            publish_state["post_publish"] = post_result
            print_section("Post-Publish Scaffolding")
            print(summarize_post_publish_actions(post_result))
            monitoring_result = run_monitoring_setup(
                workspace,
                config,
                repo_ref=repo_ref,
                args=args,
                run_command=run_command,
            )
            publish_state["monitoring"] = monitoring_result
            print_section("Post-Publish Monitoring")
            print(monitoring_result.get("summary", "monitoring: (no summary)"))

            external_monitoring_result = run_external_monitoring(
                workspace,
                config,
                repo_ref=repo_ref,
                args=args,
            )
            print_section("External Monitoring (Grafana / PagerDuty)")
            print(external_monitoring_result.get("summary", "external_monitoring: (no summary)"))
            sanitized_external = {**external_monitoring_result}
            pd_block = sanitized_external.get("pagerduty")
            if isinstance(pd_block, dict) and pd_block.get("integration_key"):
                sanitized_pd = {**pd_block}
                sanitized_pd["integration_key"] = "<redacted: rotate via gh secret PAGERDUTY_INTEGRATION_KEY>"
                sanitized_external["pagerduty"] = sanitized_pd
            publish_state["external_monitoring"] = sanitized_external

    if args.execute and not args.no_writeback:
        writeback_probe(
            gate_file=gate_file,
            config=config,
            case_dir=case_dir,
            pool_file=pool_file,
            workspace=workspace,
            candidate=candidate,
            results=probe_results,
            mode=args.mode,
            repo_ref=repo_ref,
        )
    return 0


def _run_pool_branch(args: argparse.Namespace) -> int:
    print_section("Pool Run")
    kwargs = pool_args_to_kwargs(args)
    # Resolve pool_cases_dir relative to ROOT when the user passed a relative
    # path so that ``--root /tmp/host-project`` reaches /tmp/host-project/cases.
    raw_cases_dir = Path(kwargs["pool_cases_dir"]).expanduser()
    if raw_cases_dir.is_absolute():
        cases_dir = raw_cases_dir.resolve()
    else:
        cases_dir = (ROOT / raw_cases_dir).resolve()
    cases = find_pool_cases(
        cases_dir,
        status_filter=kwargs["status_filter"],
        name_glob=kwargs["name_glob"],
    )
    print(f"Cases dir: {cases_dir}")
    print(f"Pool mode: {kwargs['pool_mode']}")
    print(f"Filter status: {kwargs['status_filter'] or '(all)'}")
    print(f"Cases discovered: {len(cases)}")
    if not cases:
        print("No cases matched filter; nothing to do.")
        return 0
    if getattr(args, "pool_auto_advance", False):
        report = run_pool_auto_advance(
            cases,
            max_workers=kwargs["max_workers"],
            run_pipeline_script=Path(__file__).resolve(),
            timeout_per_case=kwargs["timeout_per_case"],
            extra_args=kwargs.get("extra_args", []),
            max_rounds=int(getattr(args, "pool_auto_advance_max_rounds", 3)),
            include_publish=bool(getattr(args, "pool_auto_advance_include_publish", False)),
            on_round_complete=lambda rr: print(
                f"  round {rr['round']} done: groups={list(rr['groups'].keys())}"
            ),
        )
        print()
        print(format_advance_summary(report))
        return 0 if not report.get("stuck_cases") else 1
    report = run_pool_parallel(
        cases,
        mode=kwargs["pool_mode"],
        max_workers=kwargs["max_workers"],
        extra_args=kwargs.get("extra_args", []),
        run_pipeline_script=Path(__file__).resolve(),
        timeout_per_case=kwargs["timeout_per_case"],
        on_complete_callable=lambda result: print(
            f"  [{result['status']}] {result['case_dir']} "
            f"rc={result['returncode']} t={result['duration_seconds']:.1f}s"
        ),
    )
    print()
    print(format_pool_summary(report))
    return 0 if report.get("failed", 0) == 0 and report.get("timeout", 0) == 0 else 1


def main() -> int:
    args = parse_args()
    # Re-resolve ROOT now that --root has been parsed; rebind module-level
    # constants so that ``case_dir.relative_to(ROOT)`` and other ROOT-derived
    # display logic see the user-supplied root.
    global ROOT, DEFAULT_WORKSPACE_ROOT, DEFAULT_POOL_FILE
    ROOT = _resolve_root(getattr(args, "root", "") or None)
    # Self-correct ROOT when cwd drifted away from the framework root (e.g.
    # the user ``cd``-ed into a workspace and then invoked the pipeline with
    # an absolute ``--case-dir``). See ``_auto_correct_root_from_case_dir``
    # for the full rationale.
    ROOT = _auto_correct_root_from_case_dir(args, ROOT)
    DEFAULT_WORKSPACE_ROOT = ROOT / "workspaces"
    DEFAULT_POOL_FILE = ROOT / "pipeline-pool.md"
    # Materialize the --data-source flag as the env var so every downstream
    # call to ``default_data_source()`` (across enrich_candidate,
    # update_pre_build_analysis, update_gate_after_data_probe, etc.) picks up
    # the same plugin without us having to thread ``args`` everywhere.
    explicit_source = (getattr(args, "data_source", "") or "").strip()
    if explicit_source:
        try:
            get_data_source(explicit_source)
        except DataSourceError as exc:
            raise PipelineError(str(exc)) from exc
        os.environ["AGENTFLOW_DATA_SOURCE"] = explicit_source
    if args.mode == "pool":
        return _run_pool_branch(args)
    if not (args.case_dir or args.gate_file):
        raise PipelineError("Either --case-dir or --gate-file is required (except when --mode pool).")
    gate_file, config = load_gate_file(args)
    case_dir = case_dir_from_gate(gate_file)
    pool_file = Path(args.pool_file).expanduser().resolve() if args.pool_file else DEFAULT_POOL_FILE
    workspace_root = (
        Path(args.workspace_root).expanduser().resolve()
        if args.workspace_root
        else DEFAULT_WORKSPACE_ROOT
    )
    workspace = workspace_dir(config, workspace_root)
    candidate = candidate_repo(config, args.candidate_index)
    print_plan(config, gate_file, workspace, candidate)

    if args.auto_publish or args.auto_publish_dry_run:
        def _publish_callable(_args, _config, _gate_file, _case_dir, _pool_file, _workspace_root):
            _args.mode = "publish"
            _args.execute = True
            _args.allow_publish = True
            return _run_probe_or_publish_branch(_args, _config, _gate_file, _case_dir, _pool_file, _workspace_root)

        return run_auto_publish(
            args,
            config,
            gate_file,
            case_dir,
            pool_file,
            workspace_root,
            publish_workflow_callable=_publish_callable,
        )

    if args.mode == "inspect":
        return 0

    if args.mode == "discover":
        print_section("Discovery")
        query = discovery_query(config, args.discover_query)
        sources = parse_discovery_sources(args.discover_sources)
        extras = extra_sources_arg_helpers(args)
        candidates, strategy, reason = discover_candidates(
            config,
            query,
            args.discover_limit,
            sources,
            jina_query=args.jina_query,
            x_query=args.x_query,
            hackernews_query=extras.get("hackernews_query", ""),
            reddit_query=extras.get("reddit_query", ""),
            reddit_subreddits=extras.get("reddit_subreddits") or None,
        )
        print(f"Query: {query}")
        print(f"Sources: {', '.join(sources)}")
        print(f"Discovered: {len(candidates)}")
        print(f"Recommended strategy: {strategy}")
        print(f"Reason: {reason}")
        for index, item in enumerate(candidates):
            print(
                f"[{index}] source={item.get('source', '')} {item.get('name', '')} "
                f"stars={item.get('stars', 0)} "
                f"chainstream_fit={item.get('chainstream_fit_score', 0)} "
                f"fork_or_build={item.get('fork_or_build_recommendation', '')} "
                f"url={item.get('url', '')}"
            )
        if args.execute and not args.no_writeback:
            writeback_discovery(gate_file, config, case_dir, pool_file)
        return 0

    if args.mode == "data-probe":
        print_section("ChainStream Data Probe")
        data_probe_result = run_chainstream_data_probe(args, config)
        print(f"Endpoint: {data_probe_result.get('endpoint', '')}")
        print(f"Query source: {data_probe_result.get('query_source', '')}")
        print(f"Status: {data_probe_result.get('status', '')}")
        print(f"Summary: {data_probe_result.get('summary', '')}")
        if args.execute and not args.no_writeback:
            writeback_data_probe(gate_file, config, case_dir, pool_file, data_probe_result)
        return 0

    if args.mode == "kafka-probe":
        print_section("ChainStream Kafka Data Probe")
        kafka_kwargs = kafka_probe_args_from_namespace(args)
        kafka_result = run_chainstream_kafka_probe(execute=args.execute, **kafka_kwargs)
        print(f"Endpoint: {kafka_result.get('endpoint', '')}")
        print(f"Query source: {kafka_result.get('query_source', '')}")
        print(f"Status: {kafka_result.get('status', '')}")
        print(f"Summary: {kafka_result.get('summary', '')}")
        if args.execute and not args.no_writeback:
            previous_status = require_string(config, "decision", "final_status")
            update_gate_after_kafka_probe(config, kafka_result)
            evaluate_publish_readiness(config)
            new_status = require_string(config, "decision", "final_status")
            append_review_log(
                config,
                previous_status=previous_status,
                new_status=new_status,
                what_changed=f"ChainStream Kafka probe {kafka_result.get('status', 'not_run')}",
                lessons=str(kafka_result.get("summary", "")),
            )
            dump_gate_file(gate_file, config)
            update_pool_row(pool_file, config, case_dir)
            update_review_checkpoint_file(case_dir, config, candidate)
        return 0

    return _run_probe_or_publish_branch(args, config, gate_file, case_dir, pool_file, workspace_root)


def _main_entry() -> None:
    """Console-script entry point. Wraps `main()` with PipelineError handling."""
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    _main_entry()
