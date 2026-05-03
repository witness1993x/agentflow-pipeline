"""``agentflow-scan`` — single-shot multi-source hotspot scanner.

This module is a *first-class* citizen of the framework alongside
``agentflow-pipeline / agentflow-scaffold / agentflow-init``. Whereas the
pipeline ingests a *single* hotspot end-to-end (discover -> route -> probe ->
publish), ``scan_hotspots`` is a **single execution unit** for periodic market
scanning: it sweeps several free public sources (GitHub via ``gh search``,
HackerNews Algolia, Reddit JSON) for a list of queries, deduplicates across
sources, ranks by engagement, and writes a Markdown + JSON snapshot under the
host project's ``trends/`` directory.

Design goals
------------
* Pure stdlib (urllib + subprocess + json + datetime). No heavy deps.
* No secrets required. ``gh`` reuses the user's existing auth; HN/Reddit are
  open endpoints.
* Per-source failures are non-fatal — only a *fully blocked* run exits 1.
* Output path is ``<root>/trends/YYYY-MM-DD-HH/scan.{md,json}``. Different
  hours never collide; the same hour intentionally overwrites (idempotent
  cron behaviour).
* Public functions accept ``run_command`` / ``http_get_json`` injection so
  the test suite can exercise the full pipeline without real network calls.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib import error, parse, request

from .dedup_candidates import canonicalize_url


logger = logging.getLogger("agentflow.scan")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[scan] %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


USER_AGENT = "agentflow-scan/0.1"
GITHUB_URL_RE = re.compile(r"https?://github\.com/[A-Za-z0-9_.\-/]+", re.IGNORECASE)

DEFAULT_QUERIES: tuple[str, ...] = (
    "solana ai agent",
    "defi mcp server",
    "crypto trading bot",
    "ai onchain analytics",
)
DEFAULT_REDDIT_SUBREDDITS: tuple[str, ...] = (
    "ethereum",
    "solana",
    "defi",
    "cryptocurrency",
)
DEFAULT_SOURCES: tuple[str, ...] = ("github", "hackernews", "reddit")
ALL_SOURCES: frozenset[str] = frozenset(DEFAULT_SOURCES)


# --------------------------------------------------------------------------- #
# Root resolution (mirrors scaffold.py / cli.py priority)
# --------------------------------------------------------------------------- #
def _resolve_root(explicit: str | None = None) -> Path:
    """Resolve the host-project root.

    Priority: ``explicit`` -> ``AGENTFLOW_ROOT`` env -> ``Path.cwd()``.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_value = os.environ.get("AGENTFLOW_ROOT", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path.cwd().resolve()


# --------------------------------------------------------------------------- #
# Default subprocess + HTTP helpers (overridable for tests)
# --------------------------------------------------------------------------- #
def default_run_command(
    command: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Default ``run_command`` mirroring ``cli.run_command``."""
    return subprocess.run(  # noqa: S603 - intentional, args list not shell
        command, cwd=cwd, text=True, capture_output=True, check=False
    )


def _http_get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    """Fetch ``url`` and decode JSON (stdlib only).

    Raises ``RuntimeError`` on any HTTP / parse failure.
    """
    final_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    req = request.Request(url, headers=final_headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            detail = ""
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:200]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Unable to reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Timeout reaching {url}") from exc
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unable to parse JSON from {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Source 1: GitHub via `gh search repos`
# --------------------------------------------------------------------------- #
GH_SEARCH_FIELDS = (
    "name,owner,description,stargazersCount,language,createdAt,pushedAt,url"
)


def _gh_search_one(
    query: str,
    stars_min: int,
    days: int,
    limit: int,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> list[dict]:
    """Execute one ``gh search repos`` invocation and normalize the hits.

    Failures (non-zero exit, JSON parse error, etc.) are logged and return
    ``[]``; this keeps a single bad query from killing the whole scan.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).date()
    cutoff_str = cutoff.isoformat()
    full_query = f"{query} created:>{cutoff_str} stars:>{int(stars_min)}"
    command = [
        "gh",
        "search",
        "repos",
        full_query,
        "--sort",
        "stars",
        "--order",
        "desc",
        "--limit",
        str(max(1, int(limit))),
        "--json",
        GH_SEARCH_FIELDS,
    ]
    try:
        result = run_command(command)
    except FileNotFoundError as exc:
        logger.warning("gh executable not found for query %r: %s", query, exc)
        return []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("gh search crashed for query %r: %s", query, exc)
        return []
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.warning(
            "gh search failed for query %r (rc=%s): %s",
            query,
            result.returncode,
            stderr[:200],
        )
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        logger.warning("gh search returned non-json for %r: %s", query, exc)
        return []
    if not isinstance(payload, list):
        logger.warning("gh search returned non-list for %r", query)
        return []

    hits: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        owner_obj = item.get("owner") or {}
        if isinstance(owner_obj, dict):
            owner = str(owner_obj.get("login") or owner_obj.get("name") or "").strip()
        else:
            owner = str(owner_obj or "").strip()
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not url and owner and name:
            url = f"https://github.com/{owner}/{name}"
        try:
            stars = int(item.get("stargazersCount") or 0)
        except (TypeError, ValueError):
            stars = 0
        hits.append(
            {
                "source": "github",
                "query": query,
                "name": name,
                "owner": owner,
                "url": url,
                "stars": stars,
                "language": str(item.get("language") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "created_at": str(item.get("createdAt") or "").strip(),
                "pushed_at": str(item.get("pushedAt") or "").strip(),
            }
        )
    return hits


def scan_github_trending(
    queries: list[str],
    stars_min: int = 30,
    days: int = 30,
    limit_per_query: int = 10,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[dict]:
    """Scan recent GitHub repos for each query.

    Each query becomes one ``gh search repos`` invocation. Per-query failures
    are caught + logged so that the overall scan still surfaces results from
    the working queries.
    """
    runner = run_command or default_run_command
    out: list[dict] = []
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        out.extend(_gh_search_one(q, stars_min, days, limit_per_query, runner))
    return out


# --------------------------------------------------------------------------- #
# Source 2: HackerNews Algolia (no token)
# --------------------------------------------------------------------------- #
def scan_hackernews(
    queries: list[str],
    days: int = 7,
    hits_per_query: int = 10,
) -> list[dict]:
    """Scan HackerNews stories matching each query within ``days``.

    Uses the ``numericFilters=created_at_i>...`` Algolia parameter so we get
    only recent activity. Each query failure is caught + logged.
    """
    cutoff = int(
        (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).timestamp()
    )
    safe_hits = max(1, min(int(hits_per_query or 10), 50))
    out: list[dict] = []
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        params = parse.urlencode(
            {
                "query": q,
                "tags": "story",
                "hitsPerPage": str(safe_hits),
                "numericFilters": f"created_at_i>{cutoff}",
            }
        )
        url = f"https://hn.algolia.com/api/v1/search?{params}"
        try:
            payload = _http_get_json(url)
        except RuntimeError as exc:
            logger.warning("HN search failed for %r: %s", q, exc)
            continue
        if not isinstance(payload, dict):
            logger.warning("HN search returned non-object for %r", q)
            continue
        hits = payload.get("hits") or []
        if not isinstance(hits, list):
            continue
        for item in hits:
            if not isinstance(item, dict):
                continue
            object_id = str(item.get("objectID") or "").strip()
            title = str(item.get("title") or item.get("story_title") or "").strip()
            story_url = str(item.get("url") or item.get("story_url") or "").strip()
            if not story_url and object_id:
                story_url = f"https://news.ycombinator.com/item?id={object_id}"
            try:
                points = int(item.get("points") or 0)
            except (TypeError, ValueError):
                points = 0
            try:
                num_comments = int(item.get("num_comments") or 0)
            except (TypeError, ValueError):
                num_comments = 0
            out.append(
                {
                    "source": "hackernews",
                    "query": q,
                    "title": title,
                    "url": story_url,
                    "points": points,
                    "num_comments": num_comments,
                    "created_at": str(item.get("created_at") or "").strip(),
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Source 3: Reddit JSON (no token, often rate-limited)
# --------------------------------------------------------------------------- #
def _reddit_url(query: str, hits: int, subreddit: str | None) -> str:
    safe_hits = max(1, min(int(hits or 10), 100))
    params: dict[str, str] = {
        "q": query,
        "limit": str(safe_hits),
        "sort": "relevance",
    }
    if subreddit:
        params["restrict_sr"] = "on"
        return (
            f"https://www.reddit.com/r/{parse.quote(subreddit)}/search.json"
            f"?{parse.urlencode(params)}"
        )
    return f"https://www.reddit.com/search.json?{parse.urlencode(params)}"


def scan_reddit(
    queries: list[str],
    subreddits: list[str],
    hits_per_query: int = 10,
) -> list[dict]:
    """Scan Reddit (rate-limit-prone) — degrades gracefully on any failure.

    Even if every single sub-call is blocked we still return ``[]`` rather
    than raising; the aggregate layer reports it via ``by_source`` counts.
    """
    out: list[dict] = []
    targets: list[str | None] = [s.strip() for s in (subreddits or []) if s and s.strip()]
    if not targets:
        targets = [None]
    headers = {"User-Agent": USER_AGENT}

    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        seen: set[str] = set()
        for sub in targets:
            url = _reddit_url(q, hits_per_query, sub)
            try:
                payload = _http_get_json(url, headers=headers)
            except RuntimeError as exc:
                logger.warning(
                    "Reddit search failed for %r in r/%s: %s",
                    q,
                    sub or "_global_",
                    exc,
                )
                continue
            if not isinstance(payload, dict):
                continue
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                continue
            children = data.get("children") or []
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    continue
                post = child.get("data")
                if not isinstance(post, dict):
                    continue
                post_id = str(post.get("id") or "").strip()
                if post_id and post_id in seen:
                    continue
                if post_id:
                    seen.add(post_id)
                title = str(post.get("title") or "").strip()
                selftext = str(post.get("selftext") or "").strip()
                permalink = str(post.get("permalink") or "").strip()
                if permalink.startswith("/"):
                    permalink_url = f"https://reddit.com{permalink}"
                else:
                    permalink_url = permalink
                post_url = str(post.get("url") or "").strip()
                # Prefer GitHub URL embedded in selftext / url field for dedup.
                gh_url = ""
                if "github.com" in post_url.lower():
                    gh_url = post_url
                else:
                    for blob in (selftext, title):
                        m = GITHUB_URL_RE.search(blob)
                        if m:
                            gh_url = m.group(0)
                            break
                effective_url = gh_url or permalink_url or post_url
                try:
                    score = int(post.get("score") or 0)
                except (TypeError, ValueError):
                    score = 0
                try:
                    num_comments = int(post.get("num_comments") or 0)
                except (TypeError, ValueError):
                    num_comments = 0
                try:
                    created_utc = float(post.get("created_utc") or 0.0)
                except (TypeError, ValueError):
                    created_utc = 0.0
                if created_utc > 0:
                    created_at = datetime.fromtimestamp(
                        created_utc, tz=timezone.utc
                    ).isoformat()
                else:
                    created_at = ""
                out.append(
                    {
                        "source": "reddit",
                        "query": q,
                        "subreddit": str(post.get("subreddit") or sub or "").strip(),
                        "title": title,
                        "url": effective_url,
                        "score": score,
                        "num_comments": num_comments,
                        "created_at": created_at,
                    }
                )
    return out


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _engagement(item: dict) -> int:
    """Cross-source engagement score used for ranking."""
    src = item.get("source")
    if src == "github":
        try:
            return int(item.get("stars") or 0)
        except (TypeError, ValueError):
            return 0
    if src == "hackernews":
        try:
            return int(item.get("points") or 0) + int(item.get("num_comments") or 0)
        except (TypeError, ValueError):
            return 0
    if src == "reddit":
        try:
            return int(item.get("score") or 0) + int(item.get("num_comments") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _display_title(item: dict) -> str:
    src = item.get("source")
    if src == "github":
        owner = item.get("owner") or ""
        name = item.get("name") or ""
        if owner and name:
            return f"{owner}/{name}"
        return name or owner or "github-result"
    title = item.get("title") or ""
    if title:
        return title[:120]
    return item.get("url") or "result"


def _dedup_key(item: dict) -> str:
    """Stable cross-source key. GitHub URLs canonicalize to the repo base."""
    url = canonicalize_url(item.get("url", "") or "")
    if url:
        return url
    src = item.get("source", "?")
    title = (item.get("title") or item.get("name") or "").strip().lower()
    if title:
        return f"{src}:title:{title}"
    return f"{src}:{id(item)}"


def aggregate_scan_results(
    github: list[dict],
    hn: list[dict],
    reddit: list[dict],
    *,
    top_n: int = 30,
) -> dict:
    """Merge the three source lists, dedupe across sources, rank, slice top N.

    Cross-source merging keeps the *richest* representation: when a GitHub
    repo also surfaces on HN or Reddit we keep the GitHub item as the base
    (it has stars / language / description) but record every source we saw
    it in via ``sources_seen`` and sum engagement so the combined score
    pushes truly hot items higher.
    """
    by_source = {
        "github": len(github or []),
        "hackernews": len(hn or []),
        "reddit": len(reddit or []),
    }

    merged: dict[str, dict] = {}
    duplicates_merged = 0
    # Ordering: GitHub first so GitHub records win as the base entry when an
    # equivalent URL surfaces from HN / Reddit too.
    for item in list(github or []) + list(hn or []) + list(reddit or []):
        key = _dedup_key(item)
        engagement = _engagement(item)
        if key in merged:
            base = merged[key]
            duplicates_merged += 1
            seen = base.setdefault("sources_seen", [base.get("source", "")])
            src = item.get("source", "")
            if src and src not in seen:
                seen.append(src)
            base["engagement"] = int(base.get("engagement", 0)) + engagement
            # If base lacked a description but the new item has one, fill it.
            if not base.get("description") and item.get("description"):
                base["description"] = item.get("description")
            if not base.get("title") and item.get("title"):
                base["title"] = item.get("title")
            continue
        record = dict(item)
        record["engagement"] = engagement
        record["sources_seen"] = [item.get("source", "")]
        record["display_title"] = _display_title(item)
        merged[key] = record

    # Refresh display_title for merged records (in case base entry's title
    # was filled in after initial creation).
    for rec in merged.values():
        rec["display_title"] = _display_title(rec)

    all_items = sorted(
        merged.values(),
        key=lambda r: (int(r.get("engagement", 0)), r.get("source", "")),
        reverse=True,
    )
    safe_top_n = max(1, int(top_n or 30))
    top = all_items[:safe_top_n]
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "by_source": by_source,
        "unique_count": len(all_items),
        "duplicates_merged": duplicates_merged,
        "top": top,
        "all": all_items,
    }


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
def render_markdown(aggregate: dict, *, queries: Sequence[str]) -> str:
    by_source = aggregate.get("by_source") or {}
    lines: list[str] = []
    lines.append("# agentflow-scan snapshot")
    lines.append("")
    lines.append(f"- scanned_at: `{aggregate.get('scanned_at', '')}`")
    lines.append(f"- unique_count: **{aggregate.get('unique_count', 0)}**")
    lines.append(f"- duplicates_merged: {aggregate.get('duplicates_merged', 0)}")
    lines.append(
        "- by_source: "
        f"github={by_source.get('github', 0)}, "
        f"hackernews={by_source.get('hackernews', 0)}, "
        f"reddit={by_source.get('reddit', 0)}"
    )
    lines.append("- queries: " + ", ".join(f"`{q}`" for q in queries))
    lines.append("")
    lines.append("## Top results")
    lines.append("")
    top = aggregate.get("top") or []
    if not top:
        lines.append("_No results returned from any source._")
    else:
        for i, item in enumerate(top, 1):
            src = item.get("source", "?")
            eng = int(item.get("engagement", 0))
            title = item.get("display_title") or _display_title(item)
            url = item.get("url", "")
            seen = item.get("sources_seen") or [src]
            tag = src if len(seen) <= 1 else f"{src}+{','.join(s for s in seen if s != src)}"
            lines.append(
                f"{i}. [{tag}] {eng}* \"{title}\" — {url}".replace("*", "★")
            )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Top-level orchestrator
# --------------------------------------------------------------------------- #
def _hour_dirname(dt: datetime | None = None) -> str:
    now = dt or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d-%H")


def run_scan(
    queries: list[str],
    *,
    output_dir: Path,
    sources: list[str] | None = None,
    github_stars_min: int = 30,
    github_days: int = 30,
    github_limit_per_query: int = 10,
    hn_days: int = 7,
    hn_hits_per_query: int = 10,
    reddit_subreddits: list[str] | None = None,
    reddit_hits_per_query: int = 10,
    top_n: int = 30,
    formats: tuple[str, ...] = ("md", "json"),
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: datetime | None = None,
) -> dict:
    """Run a single scan + persist outputs under ``output_dir``.

    Returns the aggregate dict augmented with ``output_md_path`` /
    ``output_json_path`` (either may be ``None`` if its format wasn't
    requested) and ``output_dir`` (the per-hour subdirectory).
    """
    requested_sources = list(sources) if sources else list(DEFAULT_SOURCES)
    requested_sources = [s.strip().lower() for s in requested_sources if s.strip()]
    unknown = [s for s in requested_sources if s not in ALL_SOURCES]
    if unknown:
        logger.warning("Ignoring unknown sources: %s", unknown)
    requested_sources = [s for s in requested_sources if s in ALL_SOURCES]
    if not requested_sources:
        requested_sources = list(DEFAULT_SOURCES)

    fmts = tuple(f.strip().lower() for f in formats if f and f.strip())
    if not fmts:
        fmts = ("md", "json")

    queries = [q for q in (queries or []) if q and q.strip()]
    if not queries:
        queries = list(DEFAULT_QUERIES)

    subs = list(reddit_subreddits) if reddit_subreddits is not None else list(
        DEFAULT_REDDIT_SUBREDDITS
    )

    github_hits: list[dict] = []
    hn_hits: list[dict] = []
    reddit_hits: list[dict] = []

    if "github" in requested_sources:
        github_hits = scan_github_trending(
            queries,
            stars_min=github_stars_min,
            days=github_days,
            limit_per_query=github_limit_per_query,
            run_command=run_command,
        )
    if "hackernews" in requested_sources:
        hn_hits = scan_hackernews(
            queries, days=hn_days, hits_per_query=hn_hits_per_query
        )
    if "reddit" in requested_sources:
        reddit_hits = scan_reddit(
            queries, subreddits=subs, hits_per_query=reddit_hits_per_query
        )

    aggregate = aggregate_scan_results(github_hits, hn_hits, reddit_hits, top_n=top_n)

    hour_dir = Path(output_dir) / _hour_dirname(now)
    hour_dir.mkdir(parents=True, exist_ok=True)

    out_md: Path | None = None
    out_json: Path | None = None
    if "json" in fmts:
        out_json = hour_dir / "scan.json"
        out_json.write_text(json.dumps(aggregate, indent=2), encoding="utf-8")
    if "md" in fmts:
        out_md = hour_dir / "scan.md"
        out_md.write_text(render_markdown(aggregate, queries=queries), encoding="utf-8")

    aggregate["output_dir"] = str(hour_dir)
    aggregate["output_md_path"] = str(out_md) if out_md else None
    aggregate["output_json_path"] = str(out_json) if out_json else None
    aggregate["queries"] = list(queries)
    aggregate["sources_requested"] = requested_sources
    return aggregate


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _read_queries_file(path: str) -> list[str]:
    p = Path(path).expanduser().resolve()
    raw = p.read_text(encoding="utf-8")
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def register_scan_args(parser: argparse.ArgumentParser) -> None:
    """Register the ``agentflow-scan`` argparse interface on ``parser``."""
    parser.add_argument(
        "--queries",
        default=",".join(DEFAULT_QUERIES),
        help=(
            "Comma-separated list of search queries. "
            f"Default: {','.join(DEFAULT_QUERIES)}"
        ),
    )
    parser.add_argument(
        "--queries-file",
        default="",
        help="Optional path to a text file with one query per line. Overrides --queries.",
    )
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help=(
            "Comma-separated subset of sources. "
            f"Allowed: {','.join(sorted(ALL_SOURCES))}"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory to write trends/<hour>/scan.{md,json}. Default: <root>/trends.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=30,
        help="Number of items to include in the 'top' list. Default 30.",
    )
    parser.add_argument(
        "--github-days",
        type=int,
        default=30,
        help="GitHub created:> cutoff in days. Default 30.",
    )
    parser.add_argument(
        "--github-stars-min",
        type=int,
        default=30,
        help="GitHub stars:> cutoff. Default 30.",
    )
    parser.add_argument(
        "--github-limit-per-query",
        type=int,
        default=10,
        help="Max GitHub hits per query. Default 10.",
    )
    parser.add_argument(
        "--hn-days",
        type=int,
        default=7,
        help="HackerNews recency cutoff in days. Default 7.",
    )
    parser.add_argument(
        "--hn-hits-per-query",
        type=int,
        default=10,
        help="Max HackerNews hits per query. Default 10.",
    )
    parser.add_argument(
        "--reddit-subreddits",
        default=",".join(DEFAULT_REDDIT_SUBREDDITS),
        help=(
            "Comma-separated subreddits to scope Reddit search. "
            "Empty -> global. Default: " + ",".join(DEFAULT_REDDIT_SUBREDDITS)
        ),
    )
    parser.add_argument(
        "--reddit-hits-per-query",
        type=int,
        default=10,
        help="Max Reddit hits per query (per subreddit). Default 10.",
    )
    parser.add_argument(
        "--format",
        choices=("md", "json", "both"),
        default="both",
        help="Output format(s). Default: both.",
    )
    parser.add_argument(
        "--root",
        default="",
        help=(
            "Override host-project root (else AGENTFLOW_ROOT env, else cwd). "
            "Used to resolve --output-dir default of <root>/trends."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational output; only print final summary line.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned scan parameters without making any HTTP / gh calls.",
    )


def _resolve_output_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir).expanduser().resolve()
    root = _resolve_root(getattr(args, "root", "") or None)
    return root / "trends"


def _format_to_tuple(value: str) -> tuple[str, ...]:
    if value == "both":
        return ("md", "json")
    return (value,)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="agentflow-scan",
        description=(
            "Single-shot multi-source hotspot scanner. Sweeps GitHub / "
            "HackerNews / Reddit for the given queries and writes a "
            "trends/<YYYY-MM-DD-HH>/scan.{md,json} snapshot under the host "
            "project root."
        ),
    )
    register_scan_args(parser)
    args = parser.parse_args(argv)

    if args.quiet:
        logger.setLevel(logging.WARNING)

    if args.queries_file:
        queries = _read_queries_file(args.queries_file)
    else:
        queries = _parse_csv(args.queries) or list(DEFAULT_QUERIES)

    sources = _parse_csv(args.sources) or list(DEFAULT_SOURCES)
    subs = _parse_csv(args.reddit_subreddits)
    output_dir = _resolve_output_dir(args)
    fmts = _format_to_tuple(args.format)

    if args.dry_run:
        plan = {
            "dry_run": True,
            "queries": queries,
            "sources": sources,
            "output_dir": str(output_dir),
            "github_days": args.github_days,
            "github_stars_min": args.github_stars_min,
            "hn_days": args.hn_days,
            "reddit_subreddits": subs,
            "top_n": args.top_n,
            "format": list(fmts),
        }
        print(json.dumps(plan, indent=2))
        return 0

    aggregate = run_scan(
        queries,
        output_dir=output_dir,
        sources=sources,
        github_stars_min=args.github_stars_min,
        github_days=args.github_days,
        github_limit_per_query=args.github_limit_per_query,
        hn_days=args.hn_days,
        hn_hits_per_query=args.hn_hits_per_query,
        reddit_subreddits=subs,
        reddit_hits_per_query=args.reddit_hits_per_query,
        top_n=args.top_n,
        formats=fmts,
    )

    by_source = aggregate.get("by_source") or {}
    requested = aggregate.get("sources_requested") or sources
    requested_in = [s for s in requested if s in ALL_SOURCES]
    raw_total = sum(int(by_source.get(s, 0)) for s in requested_in)

    summary = (
        f"scanned {len(queries)} queries across {','.join(requested_in)} -> "
        f"raw={raw_total} unique={aggregate.get('unique_count', 0)} "
        f"top={len(aggregate.get('top') or [])} "
        f"out={aggregate.get('output_dir')}"
    )
    print(summary)

    if requested_in and raw_total == 0:
        # Every requested source returned zero hits — treat as fully blocked.
        logger.warning(
            "All requested sources returned zero results; treating as fully blocked."
        )
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Self-test (no real network) — `python -m agentflow_pipeline.scan_hotspots`
# --------------------------------------------------------------------------- #
def _self_test() -> None:  # pragma: no cover - executed only via __main__
    import tempfile

    failures: list[str] = []

    # Fake gh runner returning a single canned hit per query.
    class _FakeCompleted:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run_command(cmd, cwd=None):
        # Verify shape so a regression in argv composition shows up here.
        assert cmd[:3] == ["gh", "search", "repos"], cmd
        query = cmd[3]
        payload = [
            {
                "name": "demo",
                "owner": {"login": "alice"},
                "url": "https://github.com/alice/demo",
                "description": f"demo repo for query {query[:20]}",
                "stargazersCount": 123,
                "language": "Python",
                "createdAt": "2026-04-01T00:00:00Z",
                "pushedAt": "2026-04-30T00:00:00Z",
            }
        ]
        return _FakeCompleted(0, json.dumps(payload))

    def fake_http(url: str, headers=None, timeout: int = 30):
        if "hn.algolia.com" in url:
            return {
                "hits": [
                    {
                        "objectID": "777",
                        "title": "Show HN: alice/demo is cool",
                        "url": "https://github.com/alice/demo",
                        "points": 88,
                        "num_comments": 12,
                        "created_at": "2026-04-29T00:00:00Z",
                    }
                ]
            }
        # Reddit
        return {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc",
                            "subreddit": "ethereum",
                            "title": "look at alice/demo",
                            "selftext": "https://github.com/alice/demo is awesome",
                            "permalink": "/r/ethereum/comments/abc/x/",
                            "url": "https://reddit.com/r/ethereum/comments/abc/x/",
                            "score": 50,
                            "num_comments": 10,
                            "created_utc": 1714521600,
                        }
                    }
                ]
            }
        }

    this_module = sys.modules[__name__]
    original_http = this_module._http_get_json
    this_module._http_get_json = fake_http  # type: ignore[assignment]

    try:
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "trends"
            agg = run_scan(
                ["solana ai agent"],
                output_dir=outdir,
                sources=["github", "hackernews", "reddit"],
                github_stars_min=10,
                github_days=30,
                github_limit_per_query=5,
                hn_days=7,
                hn_hits_per_query=5,
                reddit_subreddits=["ethereum"],
                reddit_hits_per_query=5,
                top_n=5,
                run_command=fake_run_command,
            )

            md_path = Path(agg["output_md_path"])
            json_path = Path(agg["output_json_path"])
            if not md_path.exists():
                failures.append(f"md output missing: {md_path}")
            if not json_path.exists():
                failures.append(f"json output missing: {json_path}")
            try:
                parsed = json.loads(json_path.read_text(encoding="utf-8"))
                if parsed.get("unique_count") != 1:
                    failures.append(
                        f"expected unique_count=1 (full cross-source merge); "
                        f"got {parsed.get('unique_count')}"
                    )
                if parsed.get("duplicates_merged") != 2:
                    failures.append(
                        f"expected duplicates_merged=2; got {parsed.get('duplicates_merged')}"
                    )
                top0 = (parsed.get("top") or [{}])[0]
                if top0.get("source") != "github":
                    failures.append(
                        f"expected GitHub to be the base record after merge, got {top0.get('source')}"
                    )
                if top0.get("engagement") != 123 + (88 + 12) + (50 + 10):
                    failures.append(
                        f"expected engagement summed across sources, got {top0.get('engagement')}"
                    )
                seen = top0.get("sources_seen") or []
                if sorted(seen) != ["github", "hackernews", "reddit"]:
                    failures.append(f"sources_seen mismatch: {seen}")
            except json.JSONDecodeError as exc:
                failures.append(f"json output unparseable: {exc}")

            md_text = md_path.read_text(encoding="utf-8")
            if "agentflow-scan snapshot" not in md_text:
                failures.append("md output missing header")
            if "alice/demo" not in md_text:
                failures.append("md output missing top result owner/repo")
            if "https://github.com/alice/demo" not in md_text:
                failures.append("md output missing top result URL")
    finally:
        this_module._http_get_json = original_http  # type: ignore[assignment]

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        raise SystemExit(1)
    print("scan_hotspots self-test: all checks passed.")


if __name__ == "__main__":
    # ``python -m agentflow_pipeline.scan_hotspots --self-test`` runs the
    # offline self-test (no network); any other args dispatch to the CLI.
    if "--self-test" in sys.argv[1:]:
        _self_test()
    else:
        raise SystemExit(main())
