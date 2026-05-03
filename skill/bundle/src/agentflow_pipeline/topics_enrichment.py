#!/usr/bin/env python3
"""GitHub topics secondary-fetch and candidate enrichment.

`gh search repos --json` does not return the `topics` field, so this module
issues a follow-up `gh api repos/{owner}/{name}` call per candidate to grab
its topics and uses them to nudge the Chainstream fit score upward.

Integration patch into ``run_pipeline.py``
------------------------------------------
1. Add the import next to the other top-level imports (suggested location:
   right after the ``import yaml`` line, ~line 17). Example:

       from topics_enrichment import enrich_candidates_with_topics

2. Wire the call inside ``discover_candidates`` *after* candidates from all
   sources have been merged and sorted, but *before*
   ``update_pre_build_analysis`` runs (so the bumped fit score flows into
   the pre-build analysis). The exact line is
   ``run_pipeline.py:1133-1135``:

       candidates.sort(key=candidate_sort_key)
       # --- BEGIN PATCH ---
       enrich_stats = enrich_candidates_with_topics(
           candidates, run_command, max_calls=5
       )
       # --- END PATCH ---
       strategy, reason = recommend_strategy(config, candidates)
       update_pre_build_analysis(config, candidates)

   The ``run_command`` symbol is already defined at module scope in
   ``run_pipeline.py:241``, so it can be passed directly. ``max_calls=5``
   keeps API consumption bounded (each candidate = 1 REST call).

3. Optional: stash ``enrich_stats`` into
   ``source_context["topics_enrichment"]`` for observability:

       source_context["topics_enrichment"] = enrich_stats

This module deliberately depends only on the stdlib + a callable injected
by the caller so it can be unit-tested without touching ``subprocess``.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable


# Keywords that mark a repo as Chainstream-relevant. Each match contributes
# +5 to the candidate's chainstream_fit_score, capped at +25 per candidate.
CHAINSTREAM_TOPIC_KEYWORDS: tuple[str, ...] = (
    "chainstream",
    "blockchain",
    "dex",
    "solana",
    "ethereum",
    "web3",
    "onchain",
    "defi",
    "indexer",
    "graphql",
    "kafka",
)

PER_TOPIC_BONUS: int = 5
MAX_TOPIC_BONUS: int = 25


# ``run_command`` callable signature mirrors run_pipeline.run_command:
#     (command: list[str], cwd: Path | None = None) -> CompletedProcess[str]
RunCommand = Callable[..., "object"]


def parse_repo_owner_name(full_name: str) -> tuple[str, str]:
    """Split an ``owner/name`` string into ``(owner, name)``.

    Handles common edge cases:
      * leading/trailing whitespace and slashes
      * a leading ``https://github.com/`` URL prefix
      * trailing ``.git``
      * extra path segments (only the first two are used)

    Returns ``("", "")`` if the input cannot be parsed.
    """
    if not full_name or not isinstance(full_name, str):
        return "", ""

    text = full_name.strip()
    # Strip URL prefixes if a full URL was passed in by mistake.
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.endswith(".git"):
        text = text[: -len(".git")]
    text = text.strip("/").strip()
    if not text or "/" not in text:
        return "", ""
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        return "", ""
    owner, name = parts[0].strip(), parts[1].strip()
    if not owner or not name:
        return "", ""
    return owner, name


def fetch_repo_topics(owner: str, name: str, run_command: RunCommand) -> list[str]:
    """Fetch the ``topics`` array for a single repo via ``gh api``.

    Returns an empty list on any failure (non-zero exit, parse error,
    missing field, network blip). Never raises.
    """
    if not owner or not name:
        return []
    command = [
        "gh",
        "api",
        f"repos/{owner}/{name}",
        "--jq",
        ".topics",
    ]
    try:
        result = run_command(command)
    except Exception:
        return []

    returncode = getattr(result, "returncode", 1)
    stdout = getattr(result, "stdout", "") or ""
    if returncode != 0:
        return []

    text = stdout.strip()
    if not text:
        return []

    # ``--jq .topics`` emits a JSON array on stdout, e.g. ``["a","b"]``.
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    topics: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            cleaned = item.strip().lower()
            if cleaned:
                topics.append(cleaned)
    return topics


def _topic_bonus(topics: Iterable[str]) -> int:
    """Compute the chainstream_fit_score bonus for a topic list."""
    if not topics:
        return 0
    keyword_set = set(CHAINSTREAM_TOPIC_KEYWORDS)
    hits = 0
    for topic in topics:
        if not isinstance(topic, str):
            continue
        if topic.strip().lower() in keyword_set:
            hits += 1
    return min(hits * PER_TOPIC_BONUS, MAX_TOPIC_BONUS)


def enrich_candidates_with_topics(
    candidates: list[dict],
    run_command: RunCommand,
    max_calls: int = 5,
) -> dict:
    """Pull topics for the first ``max_calls`` GitHub candidates and bump scores.

    Mutates each enriched candidate in place by:
      * setting ``candidate["topics"]`` to the fetched list
      * adding the keyword-derived bonus to
        ``candidate["chainstream_fit_score"]``

    Only candidates whose ``source == "github_search"`` are considered; other
    sources (jina, x) typically lack an owner/name pair and are skipped.

    Returns a stats dict::

        {
            "calls_made": int,
            "topics_found_total": int,
            "candidates_enriched": int,
        }
    """
    stats = {
        "calls_made": 0,
        "topics_found_total": 0,
        "candidates_enriched": 0,
    }
    if not candidates or max_calls <= 0:
        return stats

    for candidate in candidates:
        if stats["calls_made"] >= max_calls:
            break
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("source", "")) != "github_search":
            continue
        full_name = str(candidate.get("name", "") or "").strip()
        owner, name = parse_repo_owner_name(full_name)
        if not owner or not name:
            continue

        topics = fetch_repo_topics(owner, name, run_command)
        stats["calls_made"] += 1
        candidate["topics"] = topics
        if not topics:
            continue

        stats["topics_found_total"] += len(topics)
        bonus = _topic_bonus(topics)
        if bonus > 0:
            try:
                current = int(candidate.get("chainstream_fit_score", 0) or 0)
            except (TypeError, ValueError):
                current = 0
            candidate["chainstream_fit_score"] = current + bonus
        stats["candidates_enriched"] += 1

    return stats


# ---------------------------------------------------------------------------
# Self-test: ``python topics_enrichment.py``
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from dataclasses import dataclass

    @dataclass
    class _FakeCompleted:
        returncode: int
        stdout: str
        stderr: str = ""

    # ---- parse_repo_owner_name ----
    assert parse_repo_owner_name("octo/repo") == ("octo", "repo")
    assert parse_repo_owner_name("  octo/repo  ") == ("octo", "repo")
    assert parse_repo_owner_name("https://github.com/octo/repo") == ("octo", "repo")
    assert parse_repo_owner_name("https://github.com/octo/repo.git") == ("octo", "repo")
    assert parse_repo_owner_name("git@github.com:octo/repo.git") == ("octo", "repo")
    assert parse_repo_owner_name("octo/repo/extra") == ("octo", "repo")
    assert parse_repo_owner_name("nope") == ("", "")
    assert parse_repo_owner_name("") == ("", "")
    assert parse_repo_owner_name("/leading/slash") == ("leading", "slash")
    print("parse_repo_owner_name: ok")

    # ---- fetch_repo_topics: success ----
    def _ok_run(cmd, cwd=None):
        assert cmd[:2] == ["gh", "api"]
        assert cmd[2] == "repos/foo/bar"
        return _FakeCompleted(0, '["solana","dex","misc"]')

    topics = fetch_repo_topics("foo", "bar", _ok_run)
    assert topics == ["solana", "dex", "misc"], topics

    # ---- fetch_repo_topics: empty / non-zero / bad json ----
    def _empty_run(cmd, cwd=None):
        return _FakeCompleted(0, "")
    assert fetch_repo_topics("a", "b", _empty_run) == []

    def _fail_run(cmd, cwd=None):
        return _FakeCompleted(1, "", "boom")
    assert fetch_repo_topics("a", "b", _fail_run) == []

    def _bad_json_run(cmd, cwd=None):
        return _FakeCompleted(0, "not-json")
    assert fetch_repo_topics("a", "b", _bad_json_run) == []

    def _raises_run(cmd, cwd=None):
        raise RuntimeError("subprocess died")
    assert fetch_repo_topics("a", "b", _raises_run) == []
    print("fetch_repo_topics: ok")

    # ---- enrich_candidates_with_topics ----
    # Build canned responses keyed by repo path. The fake run_command
    # returns the matching JSON or a hard failure.
    canned = {
        "repos/alice/super-dex": '["solana","dex","web3","kafka","unrelated"]',  # 4 hits -> +20
        "repos/bob/utils": '["javascript","tooling"]',                            # 0 hits -> +0
        "repos/carol/indexer": '["indexer","graphql","onchain","ethereum","defi","blockchain"]',  # 6 hits, capped +25
    }

    calls: list[list[str]] = []

    def _fake_run(cmd, cwd=None):
        calls.append(cmd)
        target = cmd[2]
        if target in canned:
            return _FakeCompleted(0, canned[target])
        return _FakeCompleted(1, "", "not found")

    candidates = [
        {
            "source": "github_search",
            "name": "alice/super-dex",
            "chainstream_fit_score": 30,
        },
        {
            "source": "jina_search",  # should be skipped
            "name": "https://example.com/x",
            "chainstream_fit_score": 10,
        },
        {
            "source": "github_search",
            "name": "bob/utils",
            "chainstream_fit_score": 12,
        },
        {
            "source": "github_search",
            "name": "carol/indexer",
            "chainstream_fit_score": 25,
        },
        {
            "source": "github_search",
            "name": "dave/missing",
            "chainstream_fit_score": 5,
        },
        # 5th github candidate hits max_calls=4 cap below
        {
            "source": "github_search",
            "name": "eve/extra",
            "chainstream_fit_score": 7,
        },
    ]

    stats = enrich_candidates_with_topics(candidates, _fake_run, max_calls=4)

    # Four gh api calls (jina entry skipped; eve never reached because cap hit).
    assert stats["calls_made"] == 4, stats
    # Total topics found: 5 (alice) + 2 (bob) + 6 (carol) + 0 (dave fails) = 13
    assert stats["topics_found_total"] == 13, stats
    # candidates_enriched counts entries that received a non-empty topic list
    # (alice, bob, carol) = 3
    assert stats["candidates_enriched"] == 3, stats

    assert candidates[0]["topics"] == ["solana", "dex", "web3", "kafka", "unrelated"]
    assert candidates[0]["chainstream_fit_score"] == 30 + 20  # 4 hits

    # Jina entry untouched
    assert "topics" not in candidates[1]
    assert candidates[1]["chainstream_fit_score"] == 10

    assert candidates[2]["topics"] == ["javascript", "tooling"]
    assert candidates[2]["chainstream_fit_score"] == 12  # no bonus

    assert candidates[3]["chainstream_fit_score"] == 25 + 25  # capped
    assert len(candidates[3]["topics"]) == 6

    # dave/missing: api failed -> empty list, no score change
    assert candidates[4]["topics"] == []
    assert candidates[4]["chainstream_fit_score"] == 5

    # eve/extra: never called because max_calls hit
    assert "topics" not in candidates[5]
    assert candidates[5]["chainstream_fit_score"] == 7

    # Verify gh api was actually invoked with the right shape
    assert calls[0] == [
        "gh",
        "api",
        "repos/alice/super-dex",
        "--jq",
        ".topics",
    ]
    print("enrich_candidates_with_topics: ok")

    # ---- empty / zero edge cases ----
    assert enrich_candidates_with_topics([], _fake_run) == {
        "calls_made": 0,
        "topics_found_total": 0,
        "candidates_enriched": 0,
    }
    assert enrich_candidates_with_topics(candidates, _fake_run, max_calls=0) == {
        "calls_made": 0,
        "topics_found_total": 0,
        "candidates_enriched": 0,
    }
    print("edge cases: ok")

    print("\nAll self-tests passed.")
