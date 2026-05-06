"""Telegram callback action handlers for the case review pipeline.

This module implements the action layer behind the Telegram inline-keyboard
buttons that operators tap on the daily review cards.  Each callback maps to a
single ``handle_<verb>`` function whose contract is:

    handle_<verb>(case_dir: Path, *, actor: str, **kwargs) -> dict

Every handler returns a dict with the keys::

    {
        "case_id":   str,
        "success":   bool,
        "summary":   str,           # <= 200 chars (Telegram answerCallbackQuery)
        "follow_up": list[dict],    # optional next-step suggestions
    }

The thin :func:`dispatch_callback_action` parses ``callback_data`` like
``case:dry-publish:HSP-005`` or ``case:snooze:HSP-005:7d``, locates the case
directory under ``<root>/cases/HSP-XXX-*/``, dispatches to the matching
handler, and **never raises** -- any handler exception is caught and surfaced
as ``success=False``.

Side-effects are designed to be idempotent: calling ``case:drop`` twice on the
same case is a no-op the second time, and ``case:write-stub`` never overwrites
an existing file in the workspace.

The optional Anthropic SDK is imported lazily; if it is not installed (or the
``ANTHROPIC_API_KEY`` env var is unset) :func:`handle_write_stub` falls back to
a deterministic static skeleton so the action still produces a usable
workspace without any network call. ``case:fork-rewrite`` is the heavier Git
case action: it prepares the candidate workspace and writes ChainStream API
client/probe scaffolding into it.
"""
from __future__ import annotations

import os
import re
import subprocess
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import yaml

from .auto_publish import check_auto_publish_safety


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASE_ID_RE = re.compile(r"^HSP-\d+$")
SNOOZE_RE = re.compile(r"^(\d{1,2})d$")
SUMMARY_MAX = 200
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_MAX_TOKENS = 4000


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    """Return an ISO-8601 timestamp with seconds precision in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(text: str, limit: int = SUMMARY_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _load_gate_yaml(case_dir: Path) -> Tuple[Path, Dict[str, Any]]:
    """Return ``(gate_path, parsed)`` for the case's pipeline-gate file."""
    gate_path = case_dir / "02-pipeline-gate.yaml"
    if not gate_path.is_file():
        raise FileNotFoundError(f"missing pipeline-gate file: {gate_path}")
    with gate_path.open("r", encoding="utf-8") as fh:
        parsed = yaml.safe_load(fh) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"unexpected pipeline-gate content: {gate_path}")
    return gate_path, parsed


def _save_gate_yaml(gate_path: Path, config: Dict[str, Any]) -> None:
    with gate_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)


def _append_review_log(config: Dict[str, Any], entry: Dict[str, Any]) -> None:
    log = config.setdefault("review_log", [])
    if not isinstance(log, list):
        # Defensive: re-normalise to a list rather than blowing up.
        config["review_log"] = [entry]
        return
    log.append(entry)


def _slug_from_dir(case_dir: Path) -> str:
    """Return the directory name (used as the workspace slug)."""
    return case_dir.name


def _case_id_from_dir(case_dir: Path) -> str:
    name = case_dir.name
    m = re.match(r"^(HSP-\d+)", name)
    return m.group(1) if m else ""


def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


# ---------------------------------------------------------------------------
# Action 1: dry-publish (read-only 8-gate safety preview)
# ---------------------------------------------------------------------------

def handle_dry_publish(case_dir: Path, *, actor: str, **kwargs: Any) -> Dict[str, Any]:
    """Run the 8-gate auto-publish safety check and report blockers.

    Never publishes; just reuses :func:`check_auto_publish_safety`.
    """
    case_id = _case_id_from_dir(case_dir)
    try:
        _, config = _load_gate_yaml(case_dir)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"dry-publish read failed: {exc}"),
            "follow_up": [],
        }

    ok, blockers = check_auto_publish_safety(config)
    follow_up: List[Dict[str, Any]] = [{
        "kind": "hint",
        "text": (
            f"运行 `agentflow-pipeline --case-dir {case_dir} "
            "--auto-publish --auto-publish-confirm` 真发布"
        ),
    }]

    if ok:
        summary = "All 8 gates passed; ready for human auto-publish-confirm."
        return {
            "case_id": case_id,
            "success": True,
            "summary": _truncate(summary),
            "follow_up": follow_up,
        }

    head = blockers[:3]
    rendered = "; ".join(head)
    suffix = ""
    if len(blockers) > 3:
        suffix = f" (+{len(blockers) - 3} more)"
    summary = _truncate(f"{len(blockers)} blockers: {rendered}{suffix}")
    follow_up.append({
        "kind": "blockers",
        "count": len(blockers),
        "items": list(blockers),
    })
    return {
        "case_id": case_id,
        "success": False,
        "summary": summary,
        "follow_up": follow_up,
    }


# ---------------------------------------------------------------------------
# Action 2: write-stub (Claude-generated or static skeleton)
# ---------------------------------------------------------------------------

def _static_skeleton_files(meta: Dict[str, Any]) -> Dict[str, str]:
    """Return {relpath: content} for the deterministic fallback skeleton.

    Uses plain string concatenation (not ``str.format``) to avoid having to
    escape every brace in the JSON / TS templates.
    """
    slug = str(meta.get("slug", "hotspot")).lower()
    hotspot_id = str(meta.get("hotspot_id", ""))
    hotspot_name = str(meta.get("hotspot_name", "Unknown hotspot"))
    project_shape = str(meta.get("project_shape", "unspecified"))
    chainstream_fit = str(meta.get("chainstream_fit", "unspecified"))
    safe_name = hotspot_name.replace('"', "'")
    description = f"Skeleton for hotspot {hotspot_id}: {safe_name}"

    package_json = (
        "{\n"
        f'  "name": "@hotspot/{slug}",\n'
        '  "version": "0.0.1",\n'
        '  "private": true,\n'
        f'  "description": "{description}",\n'
        '  "scripts": {\n'
        '    "build": "tsc -p .",\n'
        '    "test": "node --test || echo \'no tests yet\'"\n'
        "  },\n"
        '  "engines": { "node": ">=18" }\n'
        "}\n"
    )

    tsconfig_json = (
        "{\n"
        '  "compilerOptions": {\n'
        '    "target": "ES2022",\n'
        '    "module": "ES2022",\n'
        '    "moduleResolution": "Bundler",\n'
        '    "strict": true,\n'
        '    "esModuleInterop": true,\n'
        '    "outDir": "dist",\n'
        '    "rootDir": "src",\n'
        '    "skipLibCheck": true\n'
        "  },\n"
        '  "include": ["src/**/*.ts"]\n'
        "}\n"
    )

    index_ts = (
        f"// {safe_name} — minimal hello-world skeleton.\n"
        "// Flesh this out with real Chainstream logic.\n\n"
        "export interface HotspotConfig {\n"
        "  hotspotId: string;\n"
        "  hotspotName: string;\n"
        "}\n\n"
        "export function describe(config: HotspotConfig): string {\n"
        "  return `Hotspot ${config.hotspotId}: ${config.hotspotName}`;\n"
        "}\n\n"
        "if (import.meta.url === `file://${process.argv[1]}`) {\n"
        "  console.log(describe({\n"
        f'    hotspotId: "{hotspot_id}",\n'
        f'    hotspotName: "{safe_name}",\n'
        "  }));\n"
        "}\n"
    )

    readme = (
        f"# {hotspot_name}\n\n"
        "Static skeleton generated by `case:write-stub` (no Claude reasoning"
        " available).\n"
        "Set `ANTHROPIC_API_KEY` and re-run to get an AI-generated scaffold"
        " tailored to this hotspot.\n\n"
        "## Hotspot\n\n"
        f"- id: `{hotspot_id}`\n"
        f"- project shape: `{project_shape}`\n"
        f"- chainstream fit: `{chainstream_fit}`\n\n"
        "## Quickstart\n\n"
        "```bash\nnpm install\nnpm test\n```\n"
    )

    return {
        "package.json": package_json,
        "tsconfig.json": tsconfig_json,
        "src/index.ts": index_ts,
        "README.md": readme,
    }


def _try_claude_skeleton(meta: Dict[str, Any]) -> Tuple[Dict[str, str] | None, str]:
    """Attempt to generate a skeleton via the Anthropic SDK.

    Returns ``(files, mode)`` where ``files`` is a mapping of relative-path ->
    text content and ``mode`` is one of ``"claude"`` / ``"static"``. On any
    failure the function returns ``(None, "static")`` so the caller falls back
    silently.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, "static"

    try:
        # Lazy import: optional dependency, fail-soft.
        from anthropic import Anthropic  # type: ignore[import-not-found]
    except Exception:
        return None, "static"

    try:
        client = Anthropic(api_key=api_key)
    except Exception:
        return None, "static"

    prompt = (
        "Generate a minimal, runnable TypeScript skeleton for a hotspot repo.\n\n"
        f"hotspot_name: {meta.get('hotspot_name')}\n"
        f"hotspot_id:   {meta.get('hotspot_id')}\n"
        f"project_shape:{meta.get('project_shape')}\n"
        f"repo_name:    {meta.get('repo_name')}\n"
        f"chainstream_fit: {meta.get('chainstream_fit')}\n\n"
        "Produce four files only: package.json, tsconfig.json, src/index.ts, "
        "README.md.\n"
        "Format output strictly as repeating blocks:\n"
        "===FILE: <relpath>===\n<file body>\n===END===\n"
        "Do not include any other prose. Keep files small."
    )

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None, "static"

    try:
        text_chunks: List[str] = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                text_chunks.append(text)
        body = "".join(text_chunks)
    except Exception:
        return None, "static"

    files = _parse_claude_files(body)
    if not files:
        return None, "static"
    return files, "claude"


def _parse_claude_files(body: str) -> Dict[str, str]:
    """Extract ``===FILE: <path>===`` blocks from a Claude response."""
    files: Dict[str, str] = {}
    pattern = re.compile(
        r"===FILE:\s*([^=\n]+?)===\s*\n(.*?)\n===END===",
        re.DOTALL,
    )
    for match in pattern.finditer(body):
        relpath = match.group(1).strip()
        content = match.group(2)
        if relpath:
            files[relpath] = content
    return files


def _gather_stub_meta(case_dir: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    meta = config.get("meta") or {}
    repo_plan = config.get("repo_plan") or {}
    gate2 = config.get("gate_2_project_shape") or {}
    chainstream = (config.get("pre_build_analysis") or {}).get("chainstream_fit") or {}
    return {
        "slug": _slug_from_dir(case_dir),
        "hotspot_id": meta.get("hotspot_id", _case_id_from_dir(case_dir)),
        "hotspot_name": meta.get("hotspot_name", "Unknown hotspot"),
        "project_shape": gate2.get("project_shape", "unspecified"),
        "repo_name": repo_plan.get("repo_name", ""),
        "chainstream_fit": chainstream.get("verdict", "unspecified"),
    }


def handle_write_stub(case_dir: Path, *, actor: str, **kwargs: Any) -> Dict[str, Any]:
    """Materialise a TypeScript skeleton in ``<root>/workspaces/<slug>``.

    Tries the Anthropic SDK first; on any failure (missing key, missing dep,
    network error, unparseable response) falls back to a deterministic static
    skeleton so the action is never blocked. Existing files are preserved.
    """
    case_id = _case_id_from_dir(case_dir)
    root = kwargs.get("root")
    if not isinstance(root, Path):
        # Default: walk up one level from the case-dir's parent ("cases/").
        root = case_dir.parent.parent

    try:
        _, config = _load_gate_yaml(case_dir)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"write-stub read failed: {exc}"),
            "follow_up": [],
        }

    meta = _gather_stub_meta(case_dir, config)
    slug = meta["slug"]
    workspace = Path(root) / "workspaces" / slug

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        files = _static_skeleton_files(meta)
        mode = "static-no-key"
    else:
        claude_files, mode_token = _try_claude_skeleton(meta)
        if claude_files is None:
            files = _static_skeleton_files(meta)
            mode = "static-fallback"
        else:
            files = claude_files
            mode = "claude"

    workspace.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    skipped: List[str] = []
    for relpath, content in files.items():
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            skipped.append(relpath)
            continue
        target.write_text(content, encoding="utf-8")
        written.append(relpath)

    if mode == "claude":
        summary = f"Stub written to workspaces/{slug}; review then ship."
    elif mode == "static-no-key":
        summary = (
            f"wrote static skeleton (no Claude reasoning available); "
            f"set ANTHROPIC_API_KEY for AI-generated code. dir=workspaces/{slug}"
        )
    else:
        summary = (
            f"wrote static skeleton (Claude call failed); "
            f"dir=workspaces/{slug}"
        )

    follow_up: List[Dict[str, Any]] = [
        {
            "kind": "hint",
            "text": f"next: cd workspaces/{slug} && npm install && npm test",
        },
        {
            "kind": "stub_summary",
            "mode": mode,
            "written": written,
            "skipped": skipped,
            "workspace": str(workspace),
        },
    ]
    return {
        "case_id": case_id,
        "success": True,
        "summary": _truncate(summary),
        "follow_up": follow_up,
    }


# ---------------------------------------------------------------------------
# Action 3: fork-rewrite (prepare workspace + ChainStream API integration)
# ---------------------------------------------------------------------------

def _first_candidate(config: Dict[str, Any]) -> Dict[str, Any]:
    candidates = (
        (config.get("gate_3_repo_routing") or {}).get("candidate_repos") or []
    )
    for item in candidates:
        if isinstance(item, dict):
            return item
    return {}


def _chainstream_probe_query(config: Dict[str, Any]) -> tuple[str, str]:
    try:
        from .chainstream_query_builder import select_probe_target, build_probe_query

        chain_group, data_cube, _label = select_probe_target(config)
        return (
            build_probe_query(chain_group, data_cube, limit=1),
            f"chainstream:{chain_group}.{data_cube}",
        )
    except Exception:
        return (
            "query PipelineDataProbe { __schema { queryType { name } } }\n",
            "chainstream:__schema",
        )


def _chainstream_rewrite_files(config: Dict[str, Any]) -> Dict[str, str]:
    meta = config.get("meta") or {}
    gate2 = config.get("gate_2_project_shape") or {}
    query, query_source = _chainstream_probe_query(config)
    hotspot_name = str(meta.get("hotspot_name") or "Git hotspot project").replace('"', "'")
    hotspot_id = str(meta.get("hotspot_id") or "")
    project_shape = str(gate2.get("project_shape") or "data_project")
    query_literal = repr(query)

    client_ts = (
        "export interface ChainStreamGraphQLResponse<T = unknown> {\n"
        "  data?: T;\n"
        "  errors?: Array<{ message?: string }>;\n"
        "  extensions?: Record<string, unknown>;\n"
        "}\n\n"
        "export interface ChainStreamClientOptions {\n"
        "  endpoint?: string;\n"
        "  apiKey?: string;\n"
        "}\n\n"
        "const DEFAULT_ENDPOINT = 'https://graphql.chainstream.io/graphql';\n\n"
        "export function chainStreamConfig(options: ChainStreamClientOptions = {}) {\n"
        "  const endpoint = options.endpoint || process.env.CHAINSTREAM_ENDPOINT || DEFAULT_ENDPOINT;\n"
        "  const apiKey = options.apiKey || process.env.CHAINSTREAM_API_KEY || '';\n"
        "  if (!apiKey) {\n"
        "    throw new Error('CHAINSTREAM_API_KEY is required');\n"
        "  }\n"
        "  return { endpoint, apiKey };\n"
        "}\n\n"
        "export async function runChainStreamQuery<T = unknown>(\n"
        "  query: string,\n"
        "  variables: Record<string, unknown> = {},\n"
        "  options: ChainStreamClientOptions = {},\n"
        "): Promise<ChainStreamGraphQLResponse<T>> {\n"
        "  const { endpoint, apiKey } = chainStreamConfig(options);\n"
        "  const response = await fetch(endpoint, {\n"
        "    method: 'POST',\n"
        "    headers: {\n"
        "      'content-type': 'application/json',\n"
        "      'x-api-key': apiKey,\n"
        "    },\n"
        "    body: JSON.stringify({ query, variables }),\n"
        "  });\n"
        "  if (!response.ok) {\n"
        "    throw new Error(`ChainStream HTTP ${response.status}: ${await response.text()}`);\n"
        "  }\n"
        "  return (await response.json()) as ChainStreamGraphQLResponse<T>;\n"
        "}\n"
    )
    probe_ts = (
        "import { runChainStreamQuery } from './chainstream-client';\n\n"
        f"export const HOTSPOT_ID = {hotspot_id!r};\n"
        f"export const HOTSPOT_NAME = {hotspot_name!r};\n"
        f"export const PROJECT_SHAPE = {project_shape!r};\n"
        f"export const QUERY_SOURCE = {query_source!r};\n\n"
        f"export const PROBE_QUERY = {query_literal};\n\n"
        "export async function main() {\n"
        "  const result = await runChainStreamQuery(PROBE_QUERY);\n"
        "  const topKeys = Object.keys((result.data || {}) as Record<string, unknown>);\n"
        "  console.log(JSON.stringify({ hotspot: HOTSPOT_ID, querySource: QUERY_SOURCE, topKeys }, null, 2));\n"
        "}\n\n"
        "if (import.meta.url === `file://${process.argv[1]}`) {\n"
        "  main().catch((error) => {\n"
        "    console.error(error);\n"
        "    process.exitCode = 1;\n"
        "  });\n"
        "}\n"
    )
    package_json = (
        "{\n"
        f'  "name": "@agentflow/{_slug_from_hotspot(hotspot_id, hotspot_name)}",\n'
        '  "version": "0.0.1",\n'
        '  "private": true,\n'
        f'  "description": "ChainStream rewrite for {hotspot_name}",\n'
        '  "type": "module",\n'
        '  "scripts": {\n'
        '    "chainstream:probe": "tsx src/chainstream-probe.ts",\n'
        '    "build": "tsc -p tsconfig.json",\n'
        '    "test": "npm run build"\n'
        '  },\n'
        '  "devDependencies": {\n'
        '    "tsx": "^4.0.0",\n'
        '    "typescript": "^5.0.0"\n'
        '  },\n'
        '  "engines": { "node": ">=20" }\n'
        "}\n"
    )
    tsconfig = (
        "{\n"
        '  "compilerOptions": {\n'
        '    "target": "ES2022",\n'
        '    "module": "NodeNext",\n'
        '    "moduleResolution": "NodeNext",\n'
        '    "strict": true,\n'
        '    "skipLibCheck": true,\n'
        '    "outDir": "dist"\n'
        "  },\n"
        '  "include": ["src/**/*.ts"]\n'
        "}\n"
    )
    env_example = (
        "# Required for the ChainStream-backed rewrite.\n"
        "CHAINSTREAM_API_KEY=\n"
        "CHAINSTREAM_ENDPOINT=https://graphql.chainstream.io/graphql\n"
    )
    runbook = (
        "# ChainStream Rewrite Runbook\n\n"
        f"- Hotspot: `{hotspot_id}` {hotspot_name}\n"
        f"- Project shape: `{project_shape}`\n"
        f"- Probe query source: `{query_source}`\n\n"
        "## Local probe\n\n"
        "```bash\n"
        "cp .env.chainstream.example .env\n"
        "# fill CHAINSTREAM_API_KEY\n"
        "npm install\n"
        "npm run chainstream:probe\n"
        "```\n\n"
        "## Notes\n\n"
        "This rewrite adds a concrete ChainStream GraphQL client and a minimal\n"
        "limit=1 probe. Keep API keys in env/GitHub Secrets only.\n"
    )
    return {
        "src/chainstream-client.ts": client_ts,
        "src/chainstream-probe.ts": probe_ts,
        "package.json": package_json,
        "tsconfig.json": tsconfig,
        ".env.chainstream.example": env_example,
        "CHAINSTREAM_REWRITE.md": runbook,
        "chainstream/probe.graphql": query + "\n",
    }


def _slug_from_hotspot(hotspot_id: str, hotspot_name: str) -> str:
    raw = f"{hotspot_id}-{hotspot_name}".lower()
    value = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return value or "chainstream-hotspot"


def _copy_tree_into_workspace(files: Dict[str, str], workspace: Path) -> tuple[list[str], list[str]]:
    written: list[str] = []
    overwritten: list[str] = []
    for relpath, content in files.items():
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            overwritten.append(relpath)
        else:
            written.append(relpath)
        target.write_text(content, encoding="utf-8")
    return written, overwritten


def handle_fork_rewrite(case_dir: Path, *, actor: str, **kwargs: Any) -> Dict[str, Any]:
    """Clone/reuse the candidate workspace and inject ChainStream API code."""
    case_id = _case_id_from_dir(case_dir)
    root = kwargs.get("root")
    if not isinstance(root, Path):
        root = case_dir.parent.parent
    try:
        gate_path, config = _load_gate_yaml(case_dir)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"fork-rewrite read failed: {exc}"),
            "follow_up": [],
        }

    slug = _slug_from_dir(case_dir)
    workspace = Path(root) / "workspaces" / slug
    candidate = _first_candidate(config)
    candidate_url = str(candidate.get("url") or "")
    cloned = False
    if not workspace.exists() or not any(workspace.iterdir()):
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if candidate_url:
            clone = _run_command(["git", "clone", candidate_url, str(workspace)])
            if clone.returncode != 0:
                return {
                    "case_id": case_id,
                    "success": False,
                    "summary": _truncate(clone.stderr.strip() or "git clone failed"),
                    "follow_up": [],
                }
            cloned = True
        else:
            workspace.mkdir(parents=True, exist_ok=True)

    files = _chainstream_rewrite_files(config)
    written, overwritten = _copy_tree_into_workspace(files, workspace)
    now = _iso_now()
    execution = config.setdefault("execution_state", {})
    rewrite_state = execution.setdefault("chainstream_rewrite", {})
    rewrite_state.update({
        "status": "rewritten",
        "workspace": str(workspace),
        "candidate_url": candidate_url,
        "updated_at": now,
        "actor": actor,
        "written": written,
        "overwritten": overwritten,
    })
    _append_review_log(config, {
        "date": now,
        "previous_status": str((config.get("decision") or {}).get("final_status") or ""),
        "new_status": str((config.get("decision") or {}).get("final_status") or "probe"),
        "what_changed": f"ChainStream rewrite applied by {actor}; workspace={workspace}",
        "lessons": "Fork/template workspace now has ChainStream client, probe query, env example, and runbook.",
    })
    try:
        _save_gate_yaml(gate_path, config)
    except OSError as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"fork-rewrite state write failed: {exc}"),
            "follow_up": [],
        }
    summary = (
        f"{case_id} ChainStream rewrite ready in workspaces/{slug}"
        + (" (cloned)" if cloned else "")
    )
    return {
        "case_id": case_id,
        "success": True,
        "summary": _truncate(summary),
        "follow_up": [
            {"kind": "hint", "text": f"cd workspaces/{slug} && npm install && npm run chainstream:probe"},
            {
                "kind": "chainstream_rewrite",
                "workspace": str(workspace),
                "candidate_url": candidate_url,
                "written": written,
                "overwritten": overwritten,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Action 4: drop (mark case as abandoned)
# ---------------------------------------------------------------------------

def handle_drop(case_dir: Path, *, actor: str, **kwargs: Any) -> Dict[str, Any]:
    case_id = _case_id_from_dir(case_dir)
    try:
        gate_path, config = _load_gate_yaml(case_dir)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"drop read failed: {exc}"),
            "follow_up": [],
        }

    decision = config.setdefault("decision", {})
    previous_status = str(decision.get("final_status") or "")
    now = _iso_now()
    decision["final_status"] = "drop"
    decision["next_action"] = f"dropped by {actor} at {now}"

    _append_review_log(config, {
        "date": now,
        "previous_status": previous_status,
        "new_status": "drop",
        "what_changed": f"case dropped via tg callback by {actor}",
        "lessons": "",
    })

    try:
        _save_gate_yaml(gate_path, config)
    except OSError as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"drop write failed: {exc}"),
            "follow_up": [],
        }

    return {
        "case_id": case_id,
        "success": True,
        "summary": _truncate(f"{case_id} dropped."),
        "follow_up": [
            {"kind": "hint", "text": "case removed from active pool; see review_log."},
        ],
    }


# ---------------------------------------------------------------------------
# Action 4: snooze (push next_review_date by N days)
# ---------------------------------------------------------------------------

def handle_snooze(case_dir: Path, *, actor: str, days: str = "", **kwargs: Any) -> Dict[str, Any]:
    case_id = _case_id_from_dir(case_dir)
    match = SNOOZE_RE.match(days or "")
    if not match:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(
                f"invalid snooze duration: {days!r}; expected `<N>d` with 1<=N<=30."
            ),
            "follow_up": [],
        }
    n = int(match.group(1))
    if not (1 <= n <= 30):
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"snooze days out of range (1-30): {n}"),
            "follow_up": [],
        }

    try:
        gate_path, config = _load_gate_yaml(case_dir)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"snooze read failed: {exc}"),
            "follow_up": [],
        }

    new_date = (_today() + timedelta(days=n)).strftime("%Y-%m-%d")
    decision = config.setdefault("decision", {})
    previous_status = str(decision.get("final_status") or "")
    decision["next_review_date"] = new_date

    _append_review_log(config, {
        "date": _iso_now(),
        "previous_status": previous_status,
        "new_status": previous_status or "snoozed",
        "what_changed": f"snoozed for {n} days by {actor}; next_review_date={new_date}",
        "lessons": "",
    })

    try:
        _save_gate_yaml(gate_path, config)
    except OSError as exc:
        return {
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"snooze write failed: {exc}"),
            "follow_up": [],
        }

    return {
        "case_id": case_id,
        "success": True,
        "summary": _truncate(
            f"{case_id} snoozed for {n} days; next review {new_date}."
        ),
        "follow_up": [
            {"kind": "hint", "text": f"new next_review_date: {new_date}"},
        ],
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "dry-publish": handle_dry_publish,
    "write-stub": handle_write_stub,
    "fork-rewrite": handle_fork_rewrite,
    "drop": handle_drop,
    "snooze": handle_snooze,
}


def _find_case_dir(root: Path, case_id: str) -> Path | None:
    cases_root = Path(root) / "cases"
    if not cases_root.is_dir():
        return None
    matches = sorted(cases_root.glob(f"{case_id}-*"))
    for entry in matches:
        if entry.is_dir():
            return entry
    return None


def dispatch_callback_action(
    callback_data: str,
    *,
    root: Path,
    actor: str = "tg-user",
) -> Dict[str, Any]:
    """Parse ``callback_data`` and route to the matching handler.

    Format::

        <action>:<case_id>[:<extra>]

    Examples::

        case:dry-publish:HSP-005
        case:write-stub:HSP-005
        case:fork-rewrite:HSP-005
        case:drop:HSP-005
        case:snooze:HSP-005:7d
    """
    parts = (callback_data or "").split(":")
    if len(parts) < 3:
        return {
            "action": callback_data,
            "case_id": "",
            "success": False,
            "summary": _truncate(
                f"malformed callback_data: {callback_data!r}; expected >=3 parts."
            ),
            "follow_up": [],
        }

    namespace, verb, case_id, *extra = parts
    action_name = f"{namespace}:{verb}"
    if extra:
        action_name = f"{namespace}:{verb}:{case_id}"

    if namespace != "case":
        return {
            "action": action_name,
            "case_id": "",
            "success": False,
            "summary": _truncate(f"unknown namespace: {namespace!r}"),
            "follow_up": [],
        }

    if not CASE_ID_RE.match(case_id):
        return {
            "action": f"case:{verb}",
            "case_id": case_id,
            "success": False,
            "summary": _truncate(
                f"invalid case_id: {case_id!r}; expected `HSP-<digits>`."
            ),
            "follow_up": [],
        }

    case_dir = _find_case_dir(Path(root), case_id)
    if case_dir is None:
        return {
            "action": f"case:{verb}",
            "case_id": case_id,
            "success": False,
            "summary": _truncate(
                f"Case {case_id} not found under {root}/cases/"
            ),
            "follow_up": [
                {
                    "kind": "hint",
                    "text": f"check that {root}/cases/{case_id}-* exists.",
                },
            ],
        }

    handler = _HANDLERS.get(verb)
    if handler is None:
        return {
            "action": f"case:{verb}",
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"Unknown action: {verb}"),
            "follow_up": [],
        }

    handler_kwargs: Dict[str, Any] = {"root": Path(root)}
    if verb == "snooze":
        handler_kwargs["days"] = extra[0] if extra else ""

    try:
        result = handler(case_dir, actor=actor, **handler_kwargs)
    except Exception as exc:  # noqa: BLE001 - dispatch must never raise
        return {
            "action": f"case:{verb}",
            "case_id": case_id,
            "success": False,
            "summary": _truncate(f"Action handler crashed: {exc}"),
            "follow_up": [
                {"kind": "trace", "text": traceback.format_exc(limit=4)},
            ],
        }

    # Normalise: handlers may omit fields; we fill defaults.
    if not isinstance(result, dict):
        return {
            "action": f"case:{verb}",
            "case_id": case_id,
            "success": False,
            "summary": _truncate(
                f"handler returned non-dict ({type(result).__name__})"
            ),
            "follow_up": [],
        }

    out: Dict[str, Any] = {
        "action": f"case:{verb}",
        "case_id": result.get("case_id", case_id) or case_id,
        "success": bool(result.get("success", False)),
        "summary": _truncate(str(result.get("summary", ""))),
        "follow_up": list(result.get("follow_up") or []),
    }
    return out


__all__ = [
    "dispatch_callback_action",
    "handle_dry_publish",
    "handle_write_stub",
    "handle_fork_rewrite",
    "handle_drop",
    "handle_snooze",
]
