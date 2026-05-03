"""Cross-source candidate deduplication for the discovery pipeline.

This module solves the gap where the same GitHub repository surfaces from
multiple discovery sources (e.g. ``github_search`` and ``jina_search`` both
return ``github.com/owner/name``) and ends up occupying two slots in the final
candidate list.  It exposes pure-stdlib helpers that:

1. Canonicalize URLs (host case, scheme, ``.git`` suffix, trailing slash,
   ``#fragment``, ``?utm_*`` tracking parameters, GitHub ``owner/name`` keys).
2. Compute a stable per-candidate dedup key (URL > owner/name > source:title).
3. Merge two candidates that share a key, preserving the highest score, the
   union of fit reasons, the most recent timestamps and any non-empty field.
4. Run the merge over a candidate list and emit dedup statistics.

Integration patch
-----------------

Edit ``run_pipeline.py`` (do **not** modify it as part of this change; the patch
is documented here for the orchestrator to apply).

Add the import near the other local imports at the top of the file::

    from dedup_candidates import dedup_candidates

Inside ``discover_candidates`` (currently spanning lines 1083-1202) insert the
deduplication step **after every source has been collected** and **before the
first ``candidates.sort(key=candidate_sort_key)`` / topics enrichment** call.
The current code reads (around line 1173-1186)::

    candidates.sort(key=candidate_sort_key)
    topics_stats = enrich_candidates_with_topics(candidates, run_command, max_calls=5)
    candidates.sort(key=candidate_sort_key)
    ...
    source_context = ensure_nested_dict(config, "source_context")
    source_context["discovery_sources"] = source_records
    source_context["topics_enrichment"] = topics_stats

Replace the head of that block with::

    candidates, dedup_stats = dedup_candidates(candidates)
    candidates.sort(key=candidate_sort_key)
    topics_stats = enrich_candidates_with_topics(candidates, run_command, max_calls=5)
    candidates.sort(key=candidate_sort_key)
    ...
    source_context = ensure_nested_dict(config, "source_context")
    source_context["discovery_sources"] = source_records
    source_context["topics_enrichment"] = topics_stats
    source_context["dedup"] = dedup_stats

The merged candidates expose a new ``sources_seen`` list (e.g.
``["github_search", "jina_search"]``); the original ``source`` key is
preserved as the *primary* source (i.e. the source of the candidate that won
the score comparison).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "canonicalize_url",
    "candidate_dedup_key",
    "merge_candidates",
    "dedup_candidates",
]


# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------

_TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)
_TRACKING_KEYS: frozenset[str] = frozenset(
    {
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "ref_url",
    }
)


def _strip_tracking_query(query: str) -> str:
    """Remove ``utm_*`` and other common tracking params from a query string."""
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    keep = [
        (key, value)
        for key, value in pairs
        if not any(key.lower().startswith(prefix) for prefix in _TRACKING_PREFIXES)
        and key.lower() not in _TRACKING_KEYS
    ]
    return urlencode(keep, doseq=True)


def _strip_dot_git(path: str) -> str:
    if path.endswith(".git"):
        return path[: -len(".git")]
    return path


def canonicalize_url(url: str) -> str:
    """Return a canonical key for ``url``.

    Rules
    -----
    * ``http`` is upgraded to ``https``.
    * Host is lower-cased.
    * Trailing slashes (single or multiple) are stripped from the path.
    * ``.git`` suffix on the path is stripped.
    * ``#fragment`` is dropped.
    * ``?utm_*`` (and a few other well-known tracker keys) are removed.
    * For ``github.com`` URLs the canonical key collapses to
      ``https://github.com/<owner>/<name>`` regardless of any deeper path
      (sub-paths like ``/tree/main`` are dropped because they reference the
      same repository).
    * For non-GitHub URLs the rest of the URL is preserved with the rules
      above applied; query strings keep all non-tracker parameters.
    * Empty or ``None`` URLs return ``""``.
    """
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""

    # Some Jina-style hits arrive without a scheme – assume https so the rest
    # of urlsplit/urlunsplit machinery sees a netloc.
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower() or "https"
    if scheme == "http":
        scheme = "https"

    host = (parts.hostname or "").lower()
    # Drop a leading ``www.`` prefix for stability across feeds.
    if host.startswith("www."):
        host = host[4:]

    # Preserve user/port if present (rare for our sources but harmless).
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"

    path = parts.path or ""
    # Collapse repeated slashes.
    while "//" in path:
        path = path.replace("//", "/")
    path = _strip_dot_git(path)
    path = path.rstrip("/")

    if host == "github.com":
        segments = [seg for seg in path.split("/") if seg]
        if len(segments) >= 2:
            owner = segments[0].lower()
            name = _strip_dot_git(segments[1]).lower()
            return f"https://github.com/{owner}/{name}"
        # github.com/<owner> or bare github.com – return as-is (non-repo URLs
        # should still get a stable key so they are still merged together).
        return urlunsplit((scheme, netloc, path, "", ""))

    query = _strip_tracking_query(parts.query)
    # Fragment intentionally dropped.
    return urlunsplit((scheme, netloc, path, query, ""))


# ---------------------------------------------------------------------------
# Dedup keys
# ---------------------------------------------------------------------------

def _slugify_like(value: str) -> str:
    """Lowercase + strip whitespace + collapse internal spaces.

    Used for the ``owner/name`` fallback key when a candidate has no URL.
    """
    if not value:
        return ""
    cleaned = str(value).strip().lower()
    # Collapse any whitespace to a single space; the slash separator we want
    # is the ``owner/name`` slash, which is preserved as-is.
    return " ".join(cleaned.split())


def candidate_dedup_key(candidate: dict) -> str:
    """Compute a stable dedup key for ``candidate``.

    Priority order:

    1. ``canonicalize_url(candidate["url"])`` if non-empty.
    2. ``slugify_like(candidate["name"])`` (which is typically ``owner/name``).
    3. ``f"{source}:{title}"`` as a last resort.
    """
    url = canonicalize_url(candidate.get("url", "") or "")
    if url:
        return url

    name = _slugify_like(candidate.get("name", "") or "")
    if name:
        return f"name:{name}"

    source = _slugify_like(candidate.get("source", "") or "")
    title = _slugify_like(
        candidate.get("title", "")
        or candidate.get("description", "")
        or ""
    )
    if source or title:
        return f"src:{source or 'unknown'}:{title or 'untitled'}"

    # Truly empty candidate – use ``id()`` so it never collides with another
    # empty candidate but stays stable within this Python process.
    return f"anon:{id(candidate)}"


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def _coerce_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def _merge_fit_reason(primary: str, secondary: str) -> str:
    """Union of ``fit_reason`` strings (semicolon delimited, dedup, stable)."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for chunk in (primary or "").split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        key = piece.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        seen.append(piece)
    for chunk in (secondary or "").split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        key = piece.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        seen.append(piece)
    return "; ".join(seen)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _pick_recent(primary: str, secondary: str) -> str:
    """Return the lexicographically larger ISO timestamp (ISO-8601 sorts).

    Falls back to whichever side is non-empty when only one is set.
    """
    p = (primary or "").strip()
    s = (secondary or "").strip()
    if p and s:
        return p if p >= s else s
    return p or s


def _merge_sources_seen(primary: dict, secondary: dict) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()

    def _push(values: Any) -> None:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            return
        for v in values:
            if not v:
                continue
            v_str = str(v)
            if v_str in seen_set:
                continue
            seen_set.add(v_str)
            seen.append(v_str)

    _push(primary.get("sources_seen"))
    _push(primary.get("source"))
    _push(secondary.get("sources_seen"))
    _push(secondary.get("source"))
    return seen


def merge_candidates(primary: dict, secondary: dict) -> dict:
    """Merge ``secondary`` into ``primary`` and return a new dict.

    Rules
    -----
    * The candidate with the higher ``score`` is treated as the base; ties
      keep ``primary``.
    * ``sources_seen`` is the union of both sides' sources (preserves order).
    * ``source`` (singular) on the result is the primary source – i.e. the
      ``source`` of the higher-scoring candidate.
    * ``fit_reason`` is the semicolon-separated union (de-duplicated case
      insensitively, original casing preserved).
    * Any field that is empty on the base is back-filled from the other side.
    * ``pushed_at`` and ``updated_at`` are kept from whichever side has the
      more recent ISO-8601 timestamp.
    * Numeric fields (stars, forks, open_issues, *_score) take the max of
      both sides when the base value is empty/zero – this guards against
      Jina hits that often lack stats while GitHub Search has them.
    """
    primary_score = _coerce_score(primary.get("score"))
    secondary_score = _coerce_score(secondary.get("score"))

    if secondary_score > primary_score:
        base, other = secondary, primary
    else:
        base, other = primary, secondary

    merged: dict[str, Any] = dict(base)

    # Back-fill any empty field from ``other``.
    for key, value in other.items():
        if key in {"sources_seen", "fit_reason", "pushed_at", "updated_at",
                   "source", "score"}:
            continue
        if _is_empty(merged.get(key)):
            merged[key] = value

    # Numeric stat fields: prefer the larger non-zero value (handles Jina
    # zero-stats vs GitHub real stats).
    for stat_key in ("stars", "forks", "open_issues"):
        base_val = merged.get(stat_key)
        other_val = other.get(stat_key)
        try:
            base_num = float(base_val) if base_val not in (None, "") else None
        except (TypeError, ValueError):
            base_num = None
        try:
            other_num = float(other_val) if other_val not in (None, "") else None
        except (TypeError, ValueError):
            other_num = None
        if other_num is not None and (base_num is None or other_num > base_num):
            merged[stat_key] = other_val

    # Score: keep the higher of the two (base already has the higher one,
    # but make this explicit so accidental ordering does not matter).
    if secondary_score > primary_score:
        merged["score"] = secondary.get("score")
    else:
        merged["score"] = primary.get("score")

    # Timestamps – pick the more recent ISO string from either side.
    merged["pushed_at"] = _pick_recent(
        str(primary.get("pushed_at", "") or ""),
        str(secondary.get("pushed_at", "") or ""),
    )
    merged["updated_at"] = _pick_recent(
        str(primary.get("updated_at", "") or ""),
        str(secondary.get("updated_at", "") or ""),
    )

    # Fit reason: union of both sides.
    merged["fit_reason"] = _merge_fit_reason(
        str(primary.get("fit_reason", "") or ""),
        str(secondary.get("fit_reason", "") or ""),
    )

    # ``source`` stays the primary (highest score) source; ``sources_seen``
    # captures the full provenance list.
    merged["source"] = base.get("source", "") or other.get("source", "")
    merged["sources_seen"] = _merge_sources_seen(primary, secondary)

    return merged


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def dedup_candidates(candidates: list[dict]) -> tuple[list[dict], dict]:
    """Deduplicate ``candidates`` across discovery sources.

    Iterates in input order, computing a dedup key for each candidate and
    merging duplicates via :func:`merge_candidates`.  Returns a tuple
    ``(unique_candidates, stats)`` where ``stats`` follows the schema::

        {
            "input_count": int,
            "unique_count": int,
            "duplicates_merged": int,
            "by_source": {<source>: <kept_count>},
            "duplicate_examples": [
                {"key": str, "sources": [str, ...]},
                ...  # at most 3
            ],
        }

    ``by_source`` counts each unique candidate by its *primary* ``source`` (the
    one that won the score comparison after merging).
    """
    unique: dict[str, dict] = {}
    order: list[str] = []
    duplicate_examples: list[dict] = []
    duplicates_merged = 0

    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        # Always make sure each candidate carries a sources_seen list before
        # potentially being merged – simplifies downstream code.
        if "sources_seen" not in candidate or not candidate.get("sources_seen"):
            src = candidate.get("source", "")
            candidate["sources_seen"] = [src] if src else []

        key = candidate_dedup_key(candidate)
        if key in unique:
            duplicates_merged += 1
            merged = merge_candidates(unique[key], candidate)
            unique[key] = merged
            if len(duplicate_examples) < 3:
                duplicate_examples.append(
                    {"key": key, "sources": list(merged.get("sources_seen", []))}
                )
            else:
                # Update the existing example in place if the same key shows
                # up again so its source list stays accurate; otherwise leave
                # the existing examples alone (we keep the first 3 unique
                # keys).
                for example in duplicate_examples:
                    if example["key"] == key:
                        example["sources"] = list(merged.get("sources_seen", []))
                        break
        else:
            unique[key] = candidate
            order.append(key)

    unique_list = [unique[key] for key in order]

    by_source: dict[str, int] = {}
    for cand in unique_list:
        src = str(cand.get("source", "") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1

    stats = {
        "input_count": len(candidates or []),
        "unique_count": len(unique_list),
        "duplicates_merged": duplicates_merged,
        "by_source": by_source,
        "duplicate_examples": duplicate_examples,
    }
    return unique_list, stats


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _self_test() -> None:
    # 1. Same owner/name, different URL forms all canonicalize to the same key.
    forms = [
        "https://github.com/Owner/Name",
        "https://github.com/owner/name/",
        "https://github.com/owner/name.git",
        "http://github.com/owner/name",
        "https://www.github.com/owner/name",
        "https://github.com/owner/name?utm_source=newsletter&utm_medium=email",
        "https://github.com/owner/name#readme",
        "https://github.com/owner/name/tree/main",
        "github.com/owner/name",
    ]
    canon = {canonicalize_url(u) for u in forms}
    assert canon == {"https://github.com/owner/name"}, f"github canonical mismatch: {canon}"

    # 2. http -> https for non-github too.
    assert canonicalize_url("http://Example.com/Path/") == "https://example.com/Path"

    # 3. utm stripping on non-github.
    assert canonicalize_url(
        "https://example.com/x?utm_source=a&keep=1&utm_medium=b"
    ) == "https://example.com/x?keep=1"

    # 4. Empty url -> empty string.
    assert canonicalize_url("") == ""
    assert canonicalize_url(None) == ""  # type: ignore[arg-type]

    # 5. Dedup key fallback chain.
    assert candidate_dedup_key({"url": "https://github.com/owner/name.git"}) == \
        "https://github.com/owner/name"
    assert candidate_dedup_key({"url": "", "name": "Owner/Name"}) == "name:owner/name"
    assert candidate_dedup_key({"url": "", "name": "", "source": "x", "title": "T"}) \
        == "src:x:t"

    # 6. Cross-source merge: GitHub Search hit + Jina hit on same repo.
    gh_hit = {
        "source": "github_search",
        "name": "owner/name",
        "url": "https://github.com/owner/name",
        "description": "Real description",
        "stars": 100,
        "forks": 10,
        "open_issues": 2,
        "updated_at": "2025-04-01T00:00:00Z",
        "pushed_at": "2025-03-15T00:00:00Z",
        "is_archived": False,
        "is_fork": False,
        "language": "Python",
        "default_branch": "main",
        "homepage": "https://owner.example",
        "fit_reason": "matches query; high stars",
        "license_note": "MIT",
        "score": 0.82,
        "ranking_reason": "stars",
        "quality_signals": {"stars": 100},
        "chainstream_fit_score": 0.7,
        "chainstream_fit_reason": "uses solana",
        "recommended_chainstream_access": "graphql",
        "fork_or_build_recommendation": "fork",
        "fork_or_build_reason": "active",
    }
    jina_hit = {
        "source": "jina_search",
        "name": "owner/name",
        # Jina commonly returns a slightly different URL form.
        "url": "https://www.github.com/Owner/Name.git?utm_source=jina#readme",
        "description": "",  # Jina often misses descriptions
        "stars": 0,
        "forks": 0,
        "open_issues": 0,
        "updated_at": "2025-04-15T00:00:00Z",  # newer
        "pushed_at": "",
        "is_archived": False,
        "is_fork": False,
        "language": "",
        "default_branch": "",
        "homepage": "",
        "fit_reason": "matches query; jina semantic match",
        "license_note": "",
        "score": 0.55,
        "ranking_reason": "jina",
        "quality_signals": {},
        "chainstream_fit_score": 0.0,
        "chainstream_fit_reason": "",
        "recommended_chainstream_access": "",
        "fork_or_build_recommendation": "",
        "fork_or_build_reason": "",
    }

    unique, stats = dedup_candidates([gh_hit, jina_hit])
    assert len(unique) == 1, f"expected 1 unique, got {len(unique)}"
    merged = unique[0]
    assert merged["source"] == "github_search", merged["source"]
    assert set(merged["sources_seen"]) == {"github_search", "jina_search"}, merged["sources_seen"]
    assert merged["score"] == 0.82, merged["score"]
    assert merged["stars"] == 100
    assert merged["description"] == "Real description"
    assert merged["language"] == "Python"
    # Newer timestamp should win.
    assert merged["updated_at"] == "2025-04-15T00:00:00Z", merged["updated_at"]
    # pushed_at: only github had it; should be preserved.
    assert merged["pushed_at"] == "2025-03-15T00:00:00Z"
    # fit_reason: union, preserved order, deduplicated case-insensitively.
    assert "matches query" in merged["fit_reason"]
    assert "jina semantic match" in merged["fit_reason"]
    assert "high stars" in merged["fit_reason"]
    # Stats sanity.
    assert stats["input_count"] == 2
    assert stats["unique_count"] == 1
    assert stats["duplicates_merged"] == 1
    assert stats["by_source"] == {"github_search": 1}
    assert stats["duplicate_examples"] and stats["duplicate_examples"][0]["key"] \
        == "https://github.com/owner/name"

    # 7. Reverse order: lower-score primary, higher-score secondary should still
    #    promote the higher score.
    unique2, _ = dedup_candidates([jina_hit, gh_hit])
    assert len(unique2) == 1
    assert unique2[0]["score"] == 0.82
    assert unique2[0]["source"] == "github_search"
    assert set(unique2[0]["sources_seen"]) == {"github_search", "jina_search"}

    # 8. Fallback to owner/name key (no URL on either side).
    a = {"source": "x_search", "name": "Foo/Bar", "url": "", "score": 0.3,
         "fit_reason": "x signal"}
    b = {"source": "jina_search", "name": "foo/bar", "url": "", "score": 0.6,
         "fit_reason": "jina signal"}
    unique3, stats3 = dedup_candidates([a, b])
    assert len(unique3) == 1
    assert unique3[0]["source"] == "jina_search"  # higher score wins
    assert set(unique3[0]["sources_seen"]) == {"x_search", "jina_search"}
    assert stats3["duplicates_merged"] == 1

    # 9. Distinct candidates: no merge.
    c1 = {"source": "github_search", "name": "a/b",
          "url": "https://github.com/a/b", "score": 0.1}
    c2 = {"source": "github_search", "name": "c/d",
          "url": "https://github.com/c/d", "score": 0.2}
    unique4, stats4 = dedup_candidates([c1, c2])
    assert len(unique4) == 2
    assert stats4["duplicates_merged"] == 0
    assert stats4["by_source"] == {"github_search": 2}

    # 10. Three-way merge across all sources.
    x_hit = {
        "source": "x_search",
        "name": "owner/name",
        "url": "github.com/owner/name",
        "score": 0.4,
        "fit_reason": "buzz on X",
    }
    unique5, stats5 = dedup_candidates([gh_hit, jina_hit, x_hit])
    assert len(unique5) == 1
    assert set(unique5[0]["sources_seen"]) == {"github_search", "jina_search", "x_search"}
    assert stats5["duplicates_merged"] == 2
    assert "buzz on X" in unique5[0]["fit_reason"]

    # 11. Stable key for fully empty candidate (must not crash).
    empty_key = candidate_dedup_key({})
    assert empty_key.startswith("anon:")

    print("dedup_candidates self-test OK")


if __name__ == "__main__":
    _self_test()
