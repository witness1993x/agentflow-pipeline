"""Extra discovery sources for the hotspot pipeline (HackerNews + Reddit).

This module is intentionally standalone (no imports from run_pipeline) to avoid
circular dependencies. The integration into `run_pipeline.py` is a small,
well-scoped patch:

1. Top-level imports (after the existing `from topics_enrichment import ...`):

       from extra_sources import (
           ExtraSourceError,
           hackernews_search,
           normalize_hackernews_candidates,
           reddit_search,
           normalize_reddit_candidates,
           register_extra_sources_args,
           extra_sources_arg_helpers,
       )

2. Extend the `parse_discovery_sources` allowed set (around line 391):

       if "all" in requested:
           return ["github", "jina", "x", "hackernews", "reddit"]
       allowed = {"github", "jina", "x", "hackernews", "reddit"}

3. Extend `discover_candidates` signature with new optional kwargs and add two
   new branches *after* the existing `if "x" in sources:` block:

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
           ...
           if "hackernews" in sources:
               effective_query = hackernews_query or query
               try:
                   hn_raw = hackernews_search(effective_query, limit)
                   hn_candidates = normalize_hackernews_candidates(
                       hn_raw, effective_query, config,
                       enrich_callable=enrich_candidate,
                   )
                   candidates.extend(hn_candidates)
                   source_records.append({
                       "source": "hackernews",
                       "query": effective_query,
                       "status": "searched",
                       "evidence_count": len(hn_candidates),
                       "strongest_signal": hn_candidates[0].get("name", "") if hn_candidates else "",
                       "notes": "HackerNews Algolia search completed.",
                   })
               except ExtraSourceError as exc:
                   source_records.append({
                       "source": "hackernews",
                       "query": effective_query,
                       "status": "blocked",
                       "evidence_count": 0,
                       "strongest_signal": "",
                       "notes": str(exc),
                   })

           if "reddit" in sources:
               effective_query = reddit_query or query
               try:
                   rd_raw = reddit_search(effective_query, limit, reddit_subreddits)
                   rd_candidates = normalize_reddit_candidates(
                       rd_raw, effective_query, config,
                       enrich_callable=enrich_candidate,
                   )
                   candidates.extend(rd_candidates)
                   source_records.append({
                       "source": "reddit",
                       "query": effective_query,
                       "status": "searched",
                       "evidence_count": len(rd_candidates),
                       "strongest_signal": rd_candidates[0].get("name", "") if rd_candidates else "",
                       "notes": "Reddit JSON search completed.",
                   })
               except ExtraSourceError as exc:
                   source_records.append({
                       "source": "reddit",
                       "query": effective_query,
                       "status": "blocked",
                       "evidence_count": 0,
                       "strongest_signal": "",
                       "notes": str(exc),
                   })

4. In `parse_args` (after the existing `--x-query` add_argument call), add:

       register_extra_sources_args(parser)

5. In the main entry that calls `discover_candidates(...)` (around line 1973),
   pass the new kwargs:

       extras = extra_sources_arg_helpers(args)
       candidates, strategy, reason = discover_candidates(
           config, query, args.discover_limit, sources,
           jina_query=args.jina_query,
           x_query=args.x_query,
           hackernews_query=extras["hackernews_query"],
           reddit_query=extras["reddit_query"],
           reddit_subreddits=extras["reddit_subreddits"],
       )
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib import error, parse, request


USER_AGENT = "agentflow-git-repo-clone/0.1"
DEFAULT_REDDIT_SUBREDDITS = ["ethereum", "solana", "defi", "cryptocurrency"]
GITHUB_URL_RE = re.compile(r"https?://github\.com/[A-Za-z0-9_.\-/]+", re.IGNORECASE)


class ExtraSourceError(Exception):
    """Raised when a HackerNews/Reddit fetch or parse fails."""


# --------------------------------------------------------------------------- #
# HTTP helper (stdlib only, no run_pipeline dependency)
# --------------------------------------------------------------------------- #
def _http_get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
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
        raise ExtraSourceError(f"HTTP {exc.code} from {url}: {detail[:200]}") from exc
    except error.URLError as exc:
        raise ExtraSourceError(f"Unable to reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:  # pragma: no cover - defensive
        raise ExtraSourceError(f"Timeout reaching {url}") from exc
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ExtraSourceError(f"Unable to parse JSON from {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# HackerNews
# --------------------------------------------------------------------------- #
def hackernews_search(query: str, limit: int) -> list[dict]:
    """Query HN Algolia endpoint and return story hits (no token required)."""
    if not query.strip():
        return []
    safe_limit = max(1, min(int(limit or 10), 50))
    qs = parse.urlencode({"query": query, "tags": "story", "hitsPerPage": str(safe_limit)})
    url = f"https://hn.algolia.com/api/v1/search?{qs}"
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        raise ExtraSourceError("Unexpected HackerNews response shape (not an object).")
    hits = payload.get("hits", [])
    if not isinstance(hits, list):
        raise ExtraSourceError("Unexpected HackerNews response shape (hits is not a list).")
    return [item for item in hits if isinstance(item, dict)][:safe_limit]


def normalize_hackernews_candidates(
    raw_items: list[dict],
    query: str,
    config: dict,
    enrich_callable: Callable[[dict, dict], None] | None = None,
) -> list[dict]:
    candidates: list[dict] = []
    for item in raw_items:
        object_id = str(item.get("objectID", "") or "").strip()
        title = str(item.get("title") or item.get("story_title") or "").strip()
        story_url = str(item.get("url") or item.get("story_url") or "").strip()
        if not story_url:
            story_url = (
                f"https://news.ycombinator.com/item?id={object_id}" if object_id else ""
            )
        story_text = str(item.get("story_text") or "").strip()
        description_raw = title or story_text
        if title and story_text:
            description_raw = f"{title} | {story_text}"
        try:
            points = int(item.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        try:
            num_comments = int(item.get("num_comments") or 0)
        except (TypeError, ValueError):
            num_comments = 0
        engagement = points + num_comments
        name = f"hn-{object_id}" if object_id else (title[:80] or "hn-result")
        candidate = {
            "source": "hackernews",
            "name": name,
            "url": story_url,
            "description": description_raw[:500],
            "stars": engagement,
            "updated_at": str(item.get("created_at", "") or "").strip(),
            "fit_reason": f"Discovered from HackerNews search query: {query}",
            "license_note": "social_signal",
        }
        if enrich_callable is not None:
            try:
                enrich_callable(config, candidate)
            except Exception as exc:  # pragma: no cover - defensive
                # Do not let enrichment failure kill the whole pipeline.
                candidate.setdefault("fit_reason", "")
                candidate["fit_reason"] = (
                    f"{candidate['fit_reason']} (enrich_failed: {exc})"
                )
        candidates.append(candidate)
    return candidates


# --------------------------------------------------------------------------- #
# Reddit
# --------------------------------------------------------------------------- #
def _reddit_query_url(query: str, limit: int, subreddit: str | None) -> str:
    safe_limit = max(1, min(int(limit or 10), 100))
    params: dict[str, str] = {
        "q": query,
        "limit": str(safe_limit),
        "sort": "relevance",
    }
    if subreddit:
        params["restrict_sr"] = "on"
        return (
            f"https://www.reddit.com/r/{parse.quote(subreddit)}/search.json"
            f"?{parse.urlencode(params)}"
        )
    return f"https://www.reddit.com/search.json?{parse.urlencode(params)}"


def _reddit_extract_children(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        raise ExtraSourceError("Unexpected Reddit response shape (not an object).")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ExtraSourceError("Unexpected Reddit response shape (no data).")
    children = data.get("children") or []
    if not isinstance(children, list):
        raise ExtraSourceError("Unexpected Reddit response shape (children not list).")
    items: list[dict] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        post = child.get("data")
        if isinstance(post, dict):
            items.append(post)
    return items


def reddit_search(
    query: str,
    limit: int,
    subreddits: list[str] | None = None,
) -> list[dict]:
    """Query Reddit's public JSON endpoint. Requires User-Agent (set globally).

    If `subreddits` is empty / None, performs a global search. If multiple
    subreddits are provided, queries each and merges results, de-duplicated by
    post id.
    """
    if not query.strip():
        return []
    headers = {"User-Agent": USER_AGENT}
    targets: Iterable[str | None]
    if subreddits:
        targets = [s.strip() for s in subreddits if s and s.strip()]
        if not targets:
            targets = [None]
    else:
        targets = [None]

    seen: set[str] = set()
    merged: list[dict] = []
    last_error: ExtraSourceError | None = None
    success = False
    for sub in targets:
        url = _reddit_query_url(query, limit, sub)
        try:
            payload = _http_get_json(url, headers=headers)
            items = _reddit_extract_children(payload)
            success = True
        except ExtraSourceError as exc:
            last_error = exc
            continue
        for item in items:
            post_id = str(item.get("id", "") or "").strip()
            key = post_id or str(item.get("permalink", "") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    if not success and last_error is not None:
        raise last_error
    return merged[: max(1, int(limit or 10))]


def normalize_reddit_candidates(
    raw_items: list[dict],
    query: str,
    config: dict,
    enrich_callable: Callable[[dict, dict], None] | None = None,
) -> list[dict]:
    candidates: list[dict] = []
    for item in raw_items:
        post_id = str(item.get("id", "") or "").strip()
        subreddit = str(item.get("subreddit", "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        selftext = str(item.get("selftext", "") or "").strip()
        permalink = str(item.get("permalink", "") or "").strip()
        permalink_url = (
            f"https://reddit.com{permalink}"
            if permalink.startswith("/")
            else (permalink or "")
        )

        # Prefer github URL embedded in selftext / url field.
        github_match: str = ""
        post_url = str(item.get("url", "") or "").strip()
        if post_url and "github.com" in post_url.lower():
            github_match = post_url
        else:
            for blob in (selftext, title):
                m = GITHUB_URL_RE.search(blob)
                if m:
                    github_match = m.group(0)
                    break
        candidate_url = github_match or permalink_url or post_url

        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        try:
            num_comments = int(item.get("num_comments") or 0)
        except (TypeError, ValueError):
            num_comments = 0
        engagement = score + num_comments

        try:
            created_utc = float(item.get("created_utc") or 0.0)
        except (TypeError, ValueError):
            created_utc = 0.0
        if created_utc > 0:
            updated_at = (
                datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()
            )
        else:
            updated_at = ""

        description_parts: list[str] = []
        if title:
            description_parts.append(title)
        if selftext:
            description_parts.append(selftext[:300])
        description = " | ".join(description_parts)

        name = (
            f"r/{subreddit}/{post_id}"
            if subreddit and post_id
            else (title[:80] or post_id or "reddit-post")
        )
        candidate = {
            "source": "reddit",
            "name": name,
            "url": candidate_url,
            "description": description[:500],
            "stars": engagement,
            "updated_at": updated_at,
            "fit_reason": f"Discovered from Reddit r/{subreddit} query: {query}",
            "license_note": "social_signal",
        }
        if enrich_callable is not None:
            try:
                enrich_callable(config, candidate)
            except Exception as exc:  # pragma: no cover - defensive
                candidate.setdefault("fit_reason", "")
                candidate["fit_reason"] = (
                    f"{candidate['fit_reason']} (enrich_failed: {exc})"
                )
        candidates.append(candidate)
    return candidates


# --------------------------------------------------------------------------- #
# CLI integration helpers
# --------------------------------------------------------------------------- #
def register_extra_sources_args(parser: argparse.ArgumentParser) -> None:
    """Add --hackernews-query / --reddit-query / --reddit-subreddits to parser."""
    parser.add_argument(
        "--hackernews-query",
        default="",
        help="Optional HackerNews search query override. Defaults to --discover-query.",
    )
    parser.add_argument(
        "--reddit-query",
        default="",
        help="Optional Reddit search query override. Defaults to --discover-query.",
    )
    parser.add_argument(
        "--reddit-subreddits",
        default=",".join(DEFAULT_REDDIT_SUBREDDITS),
        help=(
            "Comma-separated subreddits to scope Reddit search. "
            "Empty string -> global search. Default: "
            f"{','.join(DEFAULT_REDDIT_SUBREDDITS)}"
        ),
    )


def extra_sources_arg_helpers(args: argparse.Namespace) -> dict:
    """Convert parsed args into kwargs for discover_candidates."""
    raw_subs = getattr(args, "reddit_subreddits", "") or ""
    subs = [s.strip() for s in raw_subs.split(",") if s.strip()]
    return {
        "hackernews_query": getattr(args, "hackernews_query", "") or "",
        "reddit_query": getattr(args, "reddit_query", "") or "",
        "reddit_subreddits": subs,
    }


# --------------------------------------------------------------------------- #
# Self-test (no real network)
# --------------------------------------------------------------------------- #
def _self_test() -> None:  # pragma: no cover - executed only via __main__
    import sys

    failures: list[str] = []

    # ---- Test 1: HN normalize prefers story url over HN comment link --------
    fake_hits_with_url = [
        {
            "objectID": "12345",
            "title": "Show HN: cool repo",
            "url": "https://github.com/foo/bar",
            "points": 42,
            "num_comments": 8,
            "created_at": "2026-04-01T00:00:00Z",
        }
    ]
    cands = normalize_hackernews_candidates(
        fake_hits_with_url, "test query", config={}, enrich_callable=None
    )
    if not (
        len(cands) == 1
        and cands[0]["url"] == "https://github.com/foo/bar"
        and cands[0]["source"] == "hackernews"
        and cands[0]["stars"] == 50
        and cands[0]["name"] == "hn-12345"
        and cands[0]["license_note"] == "social_signal"
    ):
        failures.append(f"HN url-priority test failed: {cands}")

    fake_hits_no_url = [
        {
            "objectID": "999",
            "title": "Ask HN: discuss",
            "url": "",
            "points": 3,
            "num_comments": 1,
            "created_at": "2026-04-02T00:00:00Z",
        }
    ]
    cands_no_url = normalize_hackernews_candidates(
        fake_hits_no_url, "q", config={}, enrich_callable=None
    )
    if not (
        len(cands_no_url) == 1
        and cands_no_url[0]["url"] == "https://news.ycombinator.com/item?id=999"
    ):
        failures.append(f"HN fallback url test failed: {cands_no_url}")

    # ---- Test 2: Reddit selftext -> github URL extraction -------------------
    fake_reddit_items = [
        {
            "id": "abc1",
            "subreddit": "ethereum",
            "title": "interesting tooling",
            "selftext": (
                "Check out https://github.com/example/repo it does "
                "a lot of cool stuff"
            ),
            "permalink": "/r/ethereum/comments/abc1/interesting/",
            "url": "https://reddit.com/r/ethereum/comments/abc1/interesting/",
            "score": 100,
            "num_comments": 25,
            "created_utc": 1714521600,
        },
        {
            "id": "def2",
            "subreddit": "solana",
            "title": "no github link here",
            "selftext": "just a discussion",
            "permalink": "/r/solana/comments/def2/foo/",
            "url": "https://reddit.com/r/solana/comments/def2/foo/",
            "score": 5,
            "num_comments": 0,
            "created_utc": 1714521600,
        },
    ]
    rd_cands = normalize_reddit_candidates(
        fake_reddit_items, "q", config={}, enrich_callable=None
    )
    if not (
        len(rd_cands) == 2
        and rd_cands[0]["url"] == "https://github.com/example/repo"
        and rd_cands[0]["source"] == "reddit"
        and rd_cands[0]["stars"] == 125
        and rd_cands[0]["name"] == "r/ethereum/abc1"
        and rd_cands[0]["license_note"] == "social_signal"
        and rd_cands[1]["url"] == "https://reddit.com/r/solana/comments/def2/foo/"
    ):
        failures.append(f"Reddit github-extract / fallback test failed: {rd_cands}")

    # ---- Test 3: reddit_search merges across subreddits & dedupes -----------
    captured_urls: list[str] = []

    def fake_http(url: str, headers: dict[str, str] | None = None, timeout: int = 30):
        captured_urls.append(url)
        # Return same id "shared1" from both subs to verify dedup, plus a unique one.
        if "/r/a/" in url:
            return {
                "data": {
                    "children": [
                        {"data": {"id": "shared1", "title": "a-title", "permalink": "/r/a/p1"}},
                        {"data": {"id": "uniq_a", "title": "uniq-a", "permalink": "/r/a/p2"}},
                    ]
                }
            }
        if "/r/b/" in url:
            return {
                "data": {
                    "children": [
                        {"data": {"id": "shared1", "title": "b-title", "permalink": "/r/b/p1"}},
                        {"data": {"id": "uniq_b", "title": "uniq-b", "permalink": "/r/b/p3"}},
                    ]
                }
            }
        return {"data": {"children": []}}

    # Monkey-patch our http helper.
    this_module = sys.modules[__name__]
    original_http = this_module._http_get_json
    this_module._http_get_json = fake_http  # type: ignore[assignment]
    try:
        merged = reddit_search("anything", limit=10, subreddits=["a", "b"])
    finally:
        this_module._http_get_json = original_http  # type: ignore[assignment]

    merged_ids = sorted(item.get("id", "") for item in merged)
    if not (
        len(merged) == 3
        and merged_ids == ["shared1", "uniq_a", "uniq_b"]
        and len(captured_urls) == 2
        and "/r/a/" in captured_urls[0]
        and "/r/b/" in captured_urls[1]
    ):
        failures.append(
            "Reddit merge/dedup test failed: "
            f"merged_ids={merged_ids} captured_urls={captured_urls}"
        )

    # ---- Test 4: reddit_search with no subreddits -> global endpoint --------
    captured_urls.clear()
    this_module._http_get_json = fake_http  # type: ignore[assignment]
    try:
        _ = reddit_search("anything", limit=5, subreddits=None)
    finally:
        this_module._http_get_json = original_http  # type: ignore[assignment]
    if not (len(captured_urls) == 1 and "/search.json" in captured_urls[0] and "/r/" not in captured_urls[0]):
        failures.append(f"Reddit global-search url test failed: {captured_urls}")

    # ---- Test 5: extra_sources_arg_helpers parses subreddit list ------------
    ns = argparse.Namespace(
        hackernews_query="hn-q",
        reddit_query="rd-q",
        reddit_subreddits="ethereum, solana ,,defi",
    )
    helpers = extra_sources_arg_helpers(ns)
    if not (
        helpers["hackernews_query"] == "hn-q"
        and helpers["reddit_query"] == "rd-q"
        and helpers["reddit_subreddits"] == ["ethereum", "solana", "defi"]
    ):
        failures.append(f"extra_sources_arg_helpers test failed: {helpers}")

    # ---- Test 6: register_extra_sources_args wires up the parser ------------
    p = argparse.ArgumentParser()
    register_extra_sources_args(p)
    parsed = p.parse_args([])
    if not (
        parsed.hackernews_query == ""
        and parsed.reddit_query == ""
        and parsed.reddit_subreddits == ",".join(DEFAULT_REDDIT_SUBREDDITS)
    ):
        failures.append(f"register_extra_sources_args defaults test failed: {parsed}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        raise SystemExit(1)
    print("extra_sources self-test: all checks passed.")


if __name__ == "__main__":
    _self_test()
