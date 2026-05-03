"""Trends history diff and new-hotspot promotion.

This module turns the per-scan ``trends/YYYY-MM-DD-HH/scan.json`` snapshots
produced by :mod:`scan_hotspots` (created by parallel agent T) into actionable
signals.  It does **three** things:

1.  Loads the rolling history of scans from a ``trends/`` directory.
2.  Diffs the latest scan against a baseline window to surface
    *new* entries (first-time appearances) and *rising* entries (engagement
    growing across multiple scans).
3.  Optionally promotes high-engagement new entries into a fresh case via the
    ``agentflow-scaffold`` console script (default dry-run).

It exposes both a Python API (``run_trends_diff`` is the top-level entry) and
the ``agentflow-trends`` console script with two sub-commands ``diff`` and
``promote``.

Design constraints
------------------

* Pure stdlib (``argparse``, ``json``, ``pathlib``, ``sys``, ``subprocess``).
* No mutation of ``scan_hotspots.py`` (parallel agent T owns it).
* URL canonicalisation reuses :func:`agentflow_pipeline.dedup_candidates.canonicalize_url`
  so the same repo URL in multiple scans always collapses to one identity.
* ``promote_to_case`` defaults to ``dry_run=True``; the CLI requires the
  explicit ``--apply`` flag to actually create case files.

Self-test (``python -m agentflow_pipeline.trends_diff``)
--------------------------------------------------------

Running the module directly creates three fake scan snapshots in a temporary
directory and exercises :func:`run_trends_diff` end-to-end, verifying that a
summary markdown is written and the return dict has sensible counts.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

from agentflow_pipeline.dedup_candidates import canonicalize_url

__all__ = [
    "load_scan_history",
    "detect_new_entries",
    "detect_rising_entries",
    "summarize_diff",
    "promote_to_case",
    "run_trends_diff",
    "main",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _entry_url(entry: dict) -> str:
    """Pull the URL field from an entry, tolerating common aliases."""
    if not isinstance(entry, dict):
        return ""
    for key in ("url", "html_url", "repo_url", "link"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _entry_key(entry: dict) -> str:
    """Stable identity key for an entry across scans.

    Falls back from canonical URL to ``owner/name`` to ``source:title`` so an
    entry without a URL still groups correctly across scans.
    """
    url = canonicalize_url(_entry_url(entry))
    if url:
        return url
    name = (entry.get("name") or entry.get("full_name") or "").strip().lower()
    if name:
        return f"name:{name}"
    title = (entry.get("title") or entry.get("description") or "").strip().lower()
    source = (entry.get("source") or "").strip().lower()
    if title or source:
        return f"src:{source or 'unknown'}:{title or 'untitled'}"
    return f"anon:{id(entry)}"


def _entry_engagement(entry: dict) -> float:
    """Best-effort engagement number for an entry.

    Looks at ``engagement`` first (scan_hotspots emits it), then falls back to
    common GitHub-ish stats so the module remains usable with ad-hoc snapshot
    formats during testing.
    """
    if not isinstance(entry, dict):
        return 0.0
    for key in ("engagement", "score", "stars", "star_count", "popularity"):
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _scan_top(scan_data: dict) -> list[dict]:
    """Extract the list of entries from a scan_data payload.

    Looks for ``top`` first (the documented key); falls back to ``entries``
    and ``hotspots`` so this stays robust if agent T renames the field later.
    """
    if not isinstance(scan_data, dict):
        return []
    for key in ("top", "entries", "hotspots", "results"):
        value = scan_data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _scan_timestamp(path: Path, scan_data: dict) -> str:
    """Resolve a scan's timestamp string.

    Prefers an explicit ``scanned_at`` inside the JSON, otherwise the parent
    directory name (which scan_hotspots names ``YYYY-MM-DD-HH``).
    """
    if isinstance(scan_data, dict):
        ts = scan_data.get("scanned_at") or scan_data.get("timestamp")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    return path.parent.name


# ---------------------------------------------------------------------------
# 1. History loading
# ---------------------------------------------------------------------------

def load_scan_history(
    trends_dir: Path,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Load every ``*/scan.json`` under ``trends_dir`` (newest first).

    Returns a list of ``{"path", "scanned_at", "scan_data"}`` records sorted
    by ``scanned_at`` descending (lexicographic on ISO/timestamp folder names
    — matches the agent T format ``YYYY-MM-DD-HH``).

    Malformed JSON files are silently skipped so a single corrupt snapshot
    cannot break the diff job.  ``limit`` truncates the resulting list (most
    recent ``limit`` entries).
    """
    trends_dir = Path(trends_dir)
    if not trends_dir.is_dir():
        return []

    records: list[dict] = []
    for scan_path in sorted(trends_dir.glob("*/scan.json")):
        try:
            scan_data = json.loads(scan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(scan_data, dict):
            continue
        records.append(
            {
                "path": scan_path,
                "scanned_at": _scan_timestamp(scan_path, scan_data),
                "scan_data": scan_data,
            }
        )

    records.sort(key=lambda r: r["scanned_at"], reverse=True)
    if limit is not None and limit >= 0:
        records = records[:limit]
    return records


# ---------------------------------------------------------------------------
# 2. New entry detection
# ---------------------------------------------------------------------------

def detect_new_entries(
    latest: dict,
    baseline_window: list[dict],
    *,
    min_engagement: int = 50,
) -> list[dict]:
    """Return entries that are *first seen* in ``latest`` (vs ``baseline_window``).

    Parameters
    ----------
    latest:
        A scan_data dict with a ``top`` list (or any of the aliased keys
        recognized by :func:`_scan_top`).
    baseline_window:
        A list of *history records* (``{path, scanned_at, scan_data}``) that
        do **not** include ``latest``.  Each record's ``scan_data.top`` list
        is scanned to build the baseline key set.
    min_engagement:
        Minimum engagement score for an entry to be considered noteworthy.
        Entries below this threshold are filtered out.

    Each returned dict is a copy of the original entry plus ``first_seen_at``
    set to ``latest["scanned_at"]`` (when present) for downstream summary use.
    """
    baseline_keys: set[str] = set()
    for record in baseline_window or []:
        scan_data = record.get("scan_data") if isinstance(record, dict) else None
        for entry in _scan_top(scan_data or {}):
            baseline_keys.add(_entry_key(entry))

    first_seen_at = ""
    if isinstance(latest, dict):
        first_seen_at = str(latest.get("scanned_at") or "")

    new_entries: list[dict] = []
    seen_in_latest: set[str] = set()
    for entry in _scan_top(latest if isinstance(latest, dict) else {}):
        key = _entry_key(entry)
        if key in baseline_keys or key in seen_in_latest:
            continue
        if _entry_engagement(entry) < float(min_engagement):
            continue
        seen_in_latest.add(key)
        copy = dict(entry)
        copy["first_seen_at"] = first_seen_at
        copy.setdefault("_dedup_key", key)
        new_entries.append(copy)
    return new_entries


# ---------------------------------------------------------------------------
# 3. Rising entry detection
# ---------------------------------------------------------------------------

def detect_rising_entries(
    history: list[dict],
    *,
    min_appearances: int = 3,
    min_engagement_growth: float = 0.5,
) -> list[dict]:
    """Find entries whose engagement is rising across the scan history.

    Parameters
    ----------
    history:
        A list of history records ordered newest-first (matching
        :func:`load_scan_history` output).
    min_appearances:
        An entry must be present in at least this many scans to qualify.
    min_engagement_growth:
        Required ratio ``(latest - oldest) / max(oldest, 1)``.  Default 0.5
        means engagement must have grown by 50% from the oldest seen scan to
        the latest seen scan.

    Returns
    -------
    A list of dicts ``{entry, engagement_history, growth_ratio, first_seen,
    current}`` sorted by ``growth_ratio`` descending.  ``engagement_history``
    is ordered oldest-first so it reads as a time series.
    """
    if not history:
        return []

    # Build per-key time series.  We iterate oldest -> newest so the series is
    # naturally chronological.
    chronological = list(reversed(history))
    series: dict[str, dict[str, Any]] = {}
    for record in chronological:
        scan_data = record.get("scan_data") if isinstance(record, dict) else None
        scanned_at = str(record.get("scanned_at") or "") if isinstance(record, dict) else ""
        if not isinstance(scan_data, dict):
            continue
        for entry in _scan_top(scan_data):
            key = _entry_key(entry)
            if not key:
                continue
            engagement = _entry_engagement(entry)
            slot = series.setdefault(
                key,
                {
                    "key": key,
                    "engagement_history": [],
                    "first_entry": entry,
                    "first_seen": scanned_at,
                    "last_entry": entry,
                    "last_seen": scanned_at,
                },
            )
            slot["engagement_history"].append(
                {"scanned_at": scanned_at, "engagement": engagement}
            )
            slot["last_entry"] = entry
            slot["last_seen"] = scanned_at

    rising: list[dict] = []
    for slot in series.values():
        history_points = slot["engagement_history"]
        if len(history_points) < min_appearances:
            continue
        oldest = history_points[0]["engagement"]
        latest_value = history_points[-1]["engagement"]
        # Use max(oldest, 1.0) so a 0 -> N case becomes a finite, comparable
        # ratio rather than infinity, while still flagging strong growth.
        denom = oldest if oldest > 0 else 1.0
        growth = (latest_value - oldest) / denom
        if growth < min_engagement_growth:
            continue
        rising.append(
            {
                "entry": slot["last_entry"],
                "engagement_history": history_points,
                "growth_ratio": growth,
                "first_seen": slot["first_seen"],
                "current": latest_value,
            }
        )

    rising.sort(key=lambda r: r["growth_ratio"], reverse=True)
    return rising


# ---------------------------------------------------------------------------
# 4. Markdown summary
# ---------------------------------------------------------------------------

def _entry_label(entry: dict) -> str:
    name = entry.get("name") or entry.get("full_name") or entry.get("title") or ""
    name = str(name).strip()
    url = _entry_url(entry)
    if name and url:
        return f"[{name}]({url})"
    if name:
        return name
    if url:
        return url
    return "(unnamed entry)"


def summarize_diff(
    new_entries: list[dict],
    rising_entries: list[dict],
) -> str:
    """Render a markdown report of new + rising entries (top 10 each)."""
    lines: list[str] = ["# Trends diff", ""]

    lines.append("## New entries")
    lines.append("")
    if not new_entries:
        lines.append("_No new entries above threshold._")
    else:
        lines.append("| # | Entry | Engagement | First seen |")
        lines.append("|---|---|---|---|")
        for index, entry in enumerate(new_entries[:10], start=1):
            engagement = _entry_engagement(entry)
            first_seen = entry.get("first_seen_at") or ""
            lines.append(
                f"| {index} | {_entry_label(entry)} | "
                f"{engagement:g} | {first_seen} |"
            )
    lines.append("")

    lines.append("## Rising entries")
    lines.append("")
    if not rising_entries:
        lines.append("_No rising entries above growth threshold._")
    else:
        lines.append("| # | Entry | Growth | Current | First seen | Appearances |")
        lines.append("|---|---|---|---|---|---|")
        for index, item in enumerate(rising_entries[:10], start=1):
            entry = item.get("entry", {})
            growth = float(item.get("growth_ratio", 0.0)) * 100.0
            current = item.get("current", 0.0)
            first_seen = item.get("first_seen", "")
            history_points = item.get("engagement_history", []) or []
            lines.append(
                f"| {index} | {_entry_label(entry)} | "
                f"{growth:+.0f}% | {current:g} | {first_seen} | "
                f"{len(history_points)} |"
            )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Promote new entry to case (via agentflow-scaffold)
# ---------------------------------------------------------------------------

def _default_scaffold_callable(argv: list[str]) -> int:
    """Invoke ``scaffold.main`` with the given argv list.

    Replaces ``sys.argv`` for the duration of the call so the existing
    argparse-based ``main`` keeps working unchanged.
    """
    from agentflow_pipeline import scaffold

    saved_argv = sys.argv
    try:
        sys.argv = ["agentflow-scaffold", *argv]
        return int(scaffold.main() or 0)
    finally:
        sys.argv = saved_argv


def promote_to_case(
    entry: dict,
    *,
    root: Path,
    owner: str = "",
    project_shape: str = "data_pipeline",
    dry_run: bool = True,
    scaffold_callable: Callable[[list[str]], int] | None = None,
) -> dict:
    """Promote ``entry`` to a fresh case via ``agentflow-scaffold``.

    Returns a dict describing the action taken: ``{hotspot_name, argv,
    dry_run, returncode}``.  When ``dry_run=True`` (the default) the
    scaffold is *not* invoked — only the planned argv is returned, which
    the CLI prints for review.
    """
    name = (
        entry.get("name")
        or entry.get("full_name")
        or entry.get("title")
        or "trending-hotspot"
    )
    name = str(name).strip() or "trending-hotspot"

    argv: list[str] = [
        "--root", str(Path(root).expanduser()),
        "--hotspot-name", name,
        "--project-shape", project_shape,
        "--status", "watch",
    ]
    if owner:
        argv.extend(["--owner", owner])
    url = _entry_url(entry)
    if url:
        argv.extend(["--thesis", f"Auto-promoted from trends scan ({url})"])
    else:
        argv.extend(["--thesis", "Auto-promoted from trends scan"])

    plan = {
        "hotspot_name": name,
        "argv": argv,
        "dry_run": bool(dry_run),
        "returncode": None,
        "url": url,
    }
    if dry_run:
        return plan

    runner = scaffold_callable or _default_scaffold_callable
    plan["returncode"] = int(runner(argv))
    return plan


# ---------------------------------------------------------------------------
# 6. Top-level orchestration
# ---------------------------------------------------------------------------

def run_trends_diff(
    trends_dir: Path,
    *,
    baseline_window_size: int = 6,
    min_new_engagement: int = 50,
    min_rising_appearances: int = 3,
    min_rising_growth: float = 0.5,
    output_dir: Path | None = None,
    auto_promote: bool = False,
    promote_min_engagement: int = 200,
    promote_owner: str = "",
    root: Path | None = None,
) -> dict:
    """Run the full diff + (optional) promote pipeline.

    Returns a dict::

        {
            "new_count": int,
            "rising_count": int,
            "promoted_count": int,
            "summary_path": str | None,
            "promoted": [...],
            "latest_scanned_at": str,
        }

    ``summary_path`` is ``None`` when there is no scan history at all.
    """
    trends_dir = Path(trends_dir)
    history = load_scan_history(trends_dir)
    if not history:
        return {
            "new_count": 0,
            "rising_count": 0,
            "promoted_count": 0,
            "summary_path": None,
            "promoted": [],
            "latest_scanned_at": "",
        }

    latest_record = history[0]
    latest_scan = latest_record["scan_data"]
    # Embed scanned_at on the latest scan dict so detect_new_entries can pass
    # it through to first_seen_at.
    if isinstance(latest_scan, dict) and "scanned_at" not in latest_scan:
        latest_scan = {**latest_scan, "scanned_at": latest_record["scanned_at"]}

    baseline = history[1 : 1 + max(0, int(baseline_window_size))]
    new_entries = detect_new_entries(
        latest_scan,
        baseline,
        min_engagement=min_new_engagement,
    )
    rising_entries = detect_rising_entries(
        history[:14],
        min_appearances=min_rising_appearances,
        min_engagement_growth=min_rising_growth,
    )

    summary_md = summarize_diff(new_entries, rising_entries)

    out_dir = Path(output_dir) if output_dir is not None else trends_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"diff-{latest_record['scanned_at']}.md"
    summary_path.write_text(summary_md, encoding="utf-8")

    promoted: list[dict] = []
    promote_root = Path(root) if root is not None else trends_dir.parent
    if auto_promote:
        for entry in new_entries:
            if _entry_engagement(entry) < float(promote_min_engagement):
                continue
            plan = promote_to_case(
                entry,
                root=promote_root,
                owner=promote_owner,
                dry_run=True,  # default still dry; CLI flips this via apply
            )
            promoted.append(plan)

    return {
        "new_count": len(new_entries),
        "rising_count": len(rising_entries),
        "promoted_count": len(promoted),
        "summary_path": str(summary_path),
        "promoted": promoted,
        "latest_scanned_at": latest_record["scanned_at"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_root(explicit: str) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    import os

    env_value = os.environ.get("AGENTFLOW_ROOT", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path.cwd().resolve()


def _resolve_trends_dir(root: Path, explicit: str) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (root / "trends").resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentflow-trends",
        description=(
            "Diff trend scan history and (optionally) auto-promote new "
            "high-engagement hotspots into agentflow cases."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diff = sub.add_parser("diff", help="Compute new + rising entries from scan history")
    diff.add_argument("--root", default="", help="Host project root (default: cwd or AGENTFLOW_ROOT)")
    diff.add_argument("--trends-dir", default="", help="Override <root>/trends")
    diff.add_argument("--baseline-window", type=int, default=6,
                      help="Number of prior scans to compare 'new' against (default 6)")
    diff.add_argument("--min-new-engagement", type=int, default=50,
                      help="Engagement threshold for new entries (default 50)")
    diff.add_argument("--output-dir", default="", help="Override summary output directory")
    diff.add_argument("--quiet", action="store_true", help="Suppress stdout summary")

    promote = sub.add_parser(
        "promote",
        help="Auto-promote new high-engagement entries to cases via agentflow-scaffold",
    )
    promote.add_argument("--root", default="", help="Host project root (default: cwd or AGENTFLOW_ROOT)")
    promote.add_argument("--trends-dir", default="", help="Override <root>/trends")
    promote.add_argument("--min-engagement", type=int, default=200,
                         help="Minimum engagement to promote (default 200)")
    promote.add_argument("--owner", default="", help="Owner field for the new case")
    promote.add_argument(
        "--project-shape",
        default="data_pipeline",
        help="Project shape (default data_pipeline)",
    )
    promote.add_argument("--apply", action="store_true",
                         help="Actually create the case (omit to dry-run)")
    promote.add_argument("--dry-run", action="store_true",
                         help="Force dry-run (overrides --apply)")
    return parser


def _cmd_diff(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    trends_dir = _resolve_trends_dir(root, args.trends_dir)
    output_dir = (
        Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    )
    result = run_trends_diff(
        trends_dir,
        baseline_window_size=args.baseline_window,
        min_new_engagement=args.min_new_engagement,
        output_dir=output_dir,
        root=root,
    )
    if not args.quiet:
        print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    trends_dir = _resolve_trends_dir(root, args.trends_dir)
    apply_real = bool(args.apply) and not bool(args.dry_run)

    history = load_scan_history(trends_dir)
    if not history:
        print(json.dumps({"promoted": [], "reason": "empty history"}, indent=2))
        return 0

    latest_record = history[0]
    latest_scan = latest_record["scan_data"]
    if isinstance(latest_scan, dict) and "scanned_at" not in latest_scan:
        latest_scan = {**latest_scan, "scanned_at": latest_record["scanned_at"]}
    baseline = history[1:7]
    new_entries = detect_new_entries(
        latest_scan,
        baseline,
        min_engagement=max(50, args.min_engagement // 4),
    )

    promoted: list[dict] = []
    for entry in new_entries:
        if _entry_engagement(entry) < float(args.min_engagement):
            continue
        plan = promote_to_case(
            entry,
            root=root,
            owner=args.owner,
            project_shape=args.project_shape,
            dry_run=not apply_real,
        )
        promoted.append(plan)

    print(json.dumps(
        {
            "apply": apply_real,
            "promoted_count": len(promoted),
            "promoted": promoted,
        },
        indent=2,
        default=str,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "diff":
        return _cmd_diff(args)
    if args.command == "promote":
        return _cmd_promote(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits before reaching here


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        trends = Path(tmp) / "trends"
        trends.mkdir(parents=True, exist_ok=True)

        def _write(folder: str, top: list[dict]) -> None:
            d = trends / folder
            d.mkdir(parents=True, exist_ok=True)
            (d / "scan.json").write_text(
                json.dumps({"scanned_at": folder, "top": top}),
                encoding="utf-8",
            )

        _write("2026-04-30-09", [
            {"name": "alice/repo", "url": "https://github.com/alice/repo", "engagement": 100},
        ])
        _write("2026-04-30-21", [
            {"name": "alice/repo", "url": "https://github.com/alice/repo", "engagement": 150},
        ])
        _write("2026-05-01-09", [
            {"name": "alice/repo", "url": "https://github.com/alice/repo", "engagement": 250},
            {"name": "bob/new", "url": "https://github.com/bob/new", "engagement": 300},
        ])

        result = run_trends_diff(trends)
        assert result["new_count"] == 1, result
        assert result["rising_count"] >= 1, result
        assert result["summary_path"] and Path(result["summary_path"]).exists()
        print("trends_diff self-test OK", result)


if __name__ == "__main__":
    _self_test()
