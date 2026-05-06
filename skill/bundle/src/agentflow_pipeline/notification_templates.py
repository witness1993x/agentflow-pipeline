"""User-customisable notification card templates (stdlib-only).

This module factors the multi-line Markdown body of the daily-scan
notification card out of :mod:`agentflow_pipeline.lark_notifier` (and
its TG-equivalent sibling) so that **operators can restyle the card
without touching framework code**.

Design principles
-----------------

* **Zero third-party deps.** We use :class:`string.Template` for
  ``$placeholder`` substitution and a hand-rolled section splitter; no
  Jinja2, no Mako, nothing else. This keeps the framework's
  ``PyYAML``-only dependency promise intact.

* **Two-layer placeholder scheme.** Plain scalars (``$brand_prefix``,
  ``$unique_count``, ``$sources_brief``, ``$top_n_actual``,
  ``$shipped_count``, ``$scanned_at_iso``, ``$scanned_at_local``,
  ``$dedup_summary``) are user-rearrangeable. Multi-line "section"
  blocks (``$top_section``, ``$shipped_section``,
  ``$promoted_section``) are framework-rendered — users can drop them
  in or omit them, but cannot customise their internal layout (that
  would balloon the template surface area).

* **Title vs body split.** The first ``\\n---\\n`` line in the
  rendered output separates the card title from the body Markdown.
  Multiple separators → only the first one splits; the rest stay in
  the body verbatim.

* **Host-project override.** Templates resolve from
  ``$AGENTFLOW_TEMPLATES_DIR/<name>.tpl`` (env override) →
  ``<host_root>/templates/notifications/<name>.tpl`` →
  bundled :data:`DEFAULT_LARK_SCAN_CARD_TPL` /
  :data:`DEFAULT_TG_SCAN_CARD_TPL`. This mirrors how the rest of the
  framework discovers user overrides.

* **``$$`` literals.** Per :class:`string.Template` semantics, a
  literal dollar sign in the template is written as ``$$``. This is
  surfaced in the doc-string of :func:`render_scan_card` so operators
  who want to write ``$5K MRR`` know the right escape.

The :func:`render_scan_card` output is **byte-for-byte equivalent** to
the legacy :func:`agentflow_pipeline.lark_notifier.notify_scan_complete`
body for every fixture exercised by the test suite (see
``tests/test_notification_templates.py``).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any


__all__ = [
    "DEFAULT_LARK_SCAN_CARD_TPL",
    "DEFAULT_TG_SCAN_CARD_TPL",
    "format_scanned_at",
    "format_top_line",
    "format_promoted_case_dir",
    "render_promoted_section",
    "render_scan_card",
    "render_shipped_section",
    "render_top_section",
    "resolve_template",
    "summarise_by_source",
]


# ---------------------------------------------------------------------------
# Bundled defaults — these strings are the source of truth for v0.3.0
# byte-for-byte parity. Hand-edit with care.
# ---------------------------------------------------------------------------
# Sentinel for "no scan content" — operators rendering empty / zero-count
# scans still get a usable card; see :func:`render_scan_card` for the
# zero-count branch which falls back to a compact legacy layout.
_ZERO_UNIQUE_TPL = (
    "$brand_prefix🔎 AgentFlow · 每日热点扫描$title_suffix\n"
    "---\n"
    "**📊 今日扫描完成: 暂无可写热点** (上游空 / filter 过窄 / 配额耗尽)\n"
    "\n"
    "sources: $sources_brief\n"
    "\n"
    "$shipped_section$scanned_at_block"
)


DEFAULT_LARK_SCAN_CARD_TPL = (
    "$brand_prefix🔎 AgentFlow · 每日热点扫描$title_suffix\n"
    "---\n"
    "**📊 扫到 $unique_count unique 候选** · sources: $sources_brief"
    "$dedup_summary\n"
    "\n"
    "**🔥 Top $top_n_actual**:\n"
    "$top_section\n"
    "\n"
    "$shipped_section"
    "$promoted_section"
    "$scanned_at_block"
)


# Telegram default mirrors the Lark layout but escapes underscores for
# MarkdownV2 friendliness in headers. Body content is pre-built to
# avoid the per-character escape minefield; users can override with
# their own ``tg_scan_card.tpl``.
DEFAULT_TG_SCAN_CARD_TPL = (
    "$brand_prefix🔎 AgentFlow · 每日热点扫描$title_suffix\n"
    "---\n"
    "*📊 扫到 $unique_count unique 候选* · sources: $sources_brief"
    "$dedup_summary\n"
    "\n"
    "*🔥 Top $top_n_actual*:\n"
    "$top_section\n"
    "\n"
    "$shipped_section"
    "$promoted_section"
    "$scanned_at_block"
)


# ---------------------------------------------------------------------------
# Section-rendering helpers (multi-line Markdown). Pure functions so they
# are trivially unit-testable in isolation.
# ---------------------------------------------------------------------------
def summarise_by_source(by_source: dict[str, Any] | None) -> str:
    """Render ``by_source`` dict as ``"gh=12, hn=4, rd=0"``.

    Returns ``"n/a"`` for empty / non-dict inputs so the rendered card
    always has *something* in the sources slot — empty strings make
    operators nervous.
    """
    if not isinstance(by_source, dict) or not by_source:
        return "n/a"
    short = {"github": "gh", "hackernews": "hn", "reddit": "rd"}
    parts: list[str] = []
    for k, v in by_source.items():
        try:
            count = int(v)
        except (TypeError, ValueError):
            count = 0
        parts.append(f"{short.get(k, k)}={count}")
    return ", ".join(parts)


def format_scanned_at(scanned_at_raw: str) -> tuple[str, str]:
    """Parse ``scanned_at`` ISO into (HH:MM local, full local ISO).

    Returns ("", "") when ``scanned_at_raw`` is falsy and ("", raw)
    when the input doesn't parse so callers always have something to
    display.
    """
    if not scanned_at_raw:
        return "", ""
    try:
        normalised = scanned_at_raw
        if normalised.endswith("Z"):
            normalised = normalised[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalised)
    except ValueError:
        return "", scanned_at_raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%H:%M"), local.isoformat()


def format_top_line(idx: int, item: dict[str, Any]) -> str:
    """Render one ranked top hit as a single Markdown list line."""
    src = str(item.get("source") or "?")
    eng = item.get("engagement")
    if eng is None:
        eng = item.get("stars") or item.get("points") or item.get("score") or 0
    try:
        eng_int = int(eng)
    except (TypeError, ValueError):
        eng_int = 0
    title = item.get("display_title") or item.get("title") or ""
    if not title:
        owner = item.get("owner") or ""
        name = item.get("name") or ""
        if owner and name:
            title = f"{owner}/{name}"
        else:
            title = item.get("url") or "(no title)"
    title = str(title)
    if len(title) > 90:
        title = title[:89] + "…"
    return f"{idx}. [{src}] {eng_int}★ `{title}`"


def format_promoted_case_dir(case_dir: str) -> str:
    """Render ``case_dir`` as a framework-relative path for the card body.

    Walks from the end so a path like ``/x/cases/cases/HSP-1`` picks
    the deepest ``cases/<...>`` segment, matching the canonical layout
    ``<framework_root>/cases/<basename>``. Falls back to ``Path.name``
    on malformed input.
    """
    if not case_dir:
        return ""
    raw = str(case_dir)
    parts = raw.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "cases" and i < len(parts) - 1:
            return "/".join(parts[i:])
    name = Path(raw).name
    return name or raw


def render_top_section(scan_result: dict, top_n: int) -> str:
    """Render the ``🔥 Top N`` body lines (no header) as a single string.

    Items past index ``top_n`` are dropped. Non-dict items are skipped
    silently. Returns ``""`` when there are no usable items so callers
    can elide the section without leaving stray blank lines.
    """
    safe_top_n = max(1, int(top_n or 5))
    top_items = [
        item for item in (scan_result.get("top") or []) if isinstance(item, dict)
    ]
    if not top_items:
        return ""
    lines: list[str] = []
    for i, item in enumerate(top_items[:safe_top_n], 1):
        lines.append(format_top_line(i, item))
    return "\n".join(lines)


def render_shipped_section(shipped_repos: list[dict]) -> str:
    """Render the ``📦 Framework 已 ship`` block.

    When ``shipped_repos`` is non-empty, returns the header + bullet
    list. When empty, returns the legacy "尚未 ship" line. The string
    has *no* trailing newline — the surrounding template controls
    spacing.
    """
    repos = [r for r in (shipped_repos or []) if isinstance(r, dict)]
    if not repos:
        return "**📦 尚未 ship 任何 repo (cases/ 下无 final_status=publish 的案例)**"

    lines: list[str] = [f"**📦 Framework 已 ship ({len(repos)})**:"]
    for repo in repos:
        name = str(repo.get("name") or "?")
        lang = str(repo.get("language") or "?") or "?"
        shape = str(repo.get("shape") or "?") or "?"
        lines.append(f"- {name} ({lang}, {shape})")
    return "\n".join(lines)


def render_promoted_section(promoted_cases: list[dict]) -> str:
    """Render the ``📝 自动 promote`` section.

    Returns ``""`` (empty string, NOT a blank line) when no cases were
    promoted — letting the surrounding template skip the section
    entirely without emitting stray ``¶`` markers.

    First three cases get explicit bullets; overflow collapses into a
    ``(+ N more)`` line so the card stays bounded regardless of input
    size.
    """
    cases = [c for c in (promoted_cases or []) if isinstance(c, dict)]
    if not cases:
        return ""

    lines: list[str] = [
        f"**📝 自动 promote 了 {len(cases)} 个新 case** (尚未写代码)"
    ]
    show = cases[:3]
    for case in show:
        hotspot_id = str(case.get("hotspot_id") or "?")
        hotspot_name = str(case.get("hotspot_name") or "")
        if len(hotspot_name) > 60:
            hotspot_name = hotspot_name[:59] + "…"
        rel = format_promoted_case_dir(str(case.get("case_dir") or ""))
        name_part = f" {hotspot_name}" if hotspot_name else ""
        if rel:
            lines.append(f"- `{hotspot_id}`{name_part} — `{rel}`")
        else:
            lines.append(f"- `{hotspot_id}`{name_part}")
    extra = len(cases) - len(show)
    if extra > 0:
        lines.append(f"- (+ {extra} more)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------
_VALID_TEMPLATE_NAMES = {"lark_scan_card", "tg_scan_card"}


def _bundled_default(name: str) -> str:
    """Return the bundled default template body for ``name``.

    Raises :class:`ValueError` for unknown names so a typo in the
    caller surfaces immediately rather than silently rendering an
    empty card.
    """
    if name == "lark_scan_card":
        return DEFAULT_LARK_SCAN_CARD_TPL
    if name == "tg_scan_card":
        return DEFAULT_TG_SCAN_CARD_TPL
    raise ValueError(
        f"unknown template name {name!r}; expected one of {sorted(_VALID_TEMPLATE_NAMES)}"
    )


def resolve_template(*, name: str, host_root: Path | None = None) -> str:
    """Look up a notification template body, falling back to the bundle.

    Resolution order (first hit wins):

    1. ``$AGENTFLOW_TEMPLATES_DIR/<name>.tpl`` when the env var is set.
    2. ``<host_root>/templates/notifications/<name>.tpl`` when
       ``host_root`` is provided.
    3. The bundled :data:`DEFAULT_LARK_SCAN_CARD_TPL` /
       :data:`DEFAULT_TG_SCAN_CARD_TPL` constant.

    The ``name`` argument must match one of the known template ids
    (``"lark_scan_card"`` / ``"tg_scan_card"``) — see
    :data:`_VALID_TEMPLATE_NAMES`. File reads use UTF-8 explicitly so
    operators can ship templates with CJK characters without surprises
    on Windows hosts.
    """
    if name not in _VALID_TEMPLATE_NAMES:
        raise ValueError(
            f"unknown template name {name!r}; expected one of {sorted(_VALID_TEMPLATE_NAMES)}"
        )

    env_dir = (os.environ.get("AGENTFLOW_TEMPLATES_DIR") or "").strip()
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir) / f"{name}.tpl")
    if host_root is not None:
        candidates.append(Path(host_root) / "templates" / "notifications" / f"{name}.tpl")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            # Permission errors / stale symlinks shouldn't kill the
            # notification path — fall back to bundled default and let
            # the operator notice via the resulting card.
            continue
    return _bundled_default(name)


# ---------------------------------------------------------------------------
# Top-level render entry point
# ---------------------------------------------------------------------------
class TemplateError(ValueError):
    """Raised when a template can't be parsed into title + body.

    Subclasses :class:`ValueError` so callers using the broad
    ``except ValueError`` pattern still catch it without having to
    import a new symbol.
    """


_TITLE_BODY_SEPARATOR = "\n---\n"


def _build_substitutions(
    *,
    scan_result: dict,
    shipped_repos: list[dict],
    auto_promoted_cases: list[dict],
    top_n: int,
    brand_prefix: str,
    top_section: str,
    shipped_section: str,
    promoted_section: str,
) -> dict[str, str]:
    """Build the substitution dict for both the zero and non-zero paths.

    Keeping this isolated lets the zero-count branch reuse the same
    scalar formatting (sources_brief, scanned_at) without re-deriving
    them from ``scan_result``.
    """
    unique_count = int(scan_result.get("unique_count") or 0)
    duplicates_merged = int(scan_result.get("duplicates_merged") or 0)
    by_source = scan_result.get("by_source") or {}
    scanned_at_raw = str(scan_result.get("scanned_at") or "")
    hh_mm, full_local = format_scanned_at(scanned_at_raw)

    safe_top_n = max(1, int(top_n or 5))
    top_items = [
        item for item in (scan_result.get("top") or []) if isinstance(item, dict)
    ]
    top_n_actual = min(safe_top_n, len(top_items))

    title_suffix = f" ({hh_mm})" if hh_mm else ""

    # ``$dedup_summary`` is rendered as a leading newline + content so
    # operators writing ``... · sources: $sources_brief$dedup_summary``
    # in their template get a clean blank line when dedup is non-zero
    # and nothing at all when it's zero.
    dedup_summary = (
        f"\n_合并掉 {duplicates_merged} 条跨源重复_"
        if duplicates_merged
        else ""
    )

    # Promoted section is rendered with a *leading* blank line so the
    # template can write ``...$shipped_section$promoted_section`` and
    # get the right spacing whether promoted is empty or not.
    if promoted_section:
        promoted_block = "\n\n" + promoted_section
    else:
        promoted_block = ""

    # ``scanned_at`` line gets the same lazy-spacing treatment.
    scanned_at_block = f"\n\nscanned_at: `{full_local}`" if full_local else ""

    # Lark / TG title prefix is OPT-IN at the template level: when the
    # caller passes a non-empty brand_prefix, we surface the rendered
    # ``[X] `` token. Empty string → empty substitution.
    if brand_prefix:
        inner = brand_prefix.strip().strip("[] ")
        brand_prefix_title = f"[{inner}] " if inner else ""
    else:
        brand_prefix_title = ""

    return {
        "brand_prefix": brand_prefix_title,
        "title_suffix": title_suffix,
        "scanned_at_local": hh_mm,
        "scanned_at_iso": full_local,
        "scanned_at_block": scanned_at_block,
        "unique_count": str(unique_count),
        "sources_brief": summarise_by_source(by_source),
        "dedup_summary": dedup_summary,
        "top_n_actual": str(top_n_actual),
        "shipped_count": str(len([r for r in shipped_repos if isinstance(r, dict)])),
        "top_section": top_section,
        "shipped_section": shipped_section,
        "promoted_section": promoted_block,
    }


def render_scan_card(
    *,
    template: str,
    scan_result: dict,
    shipped_repos: list[dict],
    auto_promoted_cases: list[dict],
    top_n: int = 5,
    brand_prefix: str = "",
) -> tuple[str, str]:
    """Render ``template`` into a ``(title, body_md)`` pair.

    The first ``\\n---\\n`` line in the rendered output is treated as
    the title/body separator; everything before it is the title,
    everything after is the body. Multiple separators are *not*
    re-split — the second and onward stay verbatim in the body.

    Substitution semantics
    ----------------------
    * Plain scalar placeholders (``$brand_prefix``, ``$unique_count``,
      ``$sources_brief``, ``$top_n_actual``, ``$shipped_count``,
      ``$scanned_at_iso``, ``$scanned_at_local``, ``$dedup_summary``,
      ``$title_suffix``, ``$scanned_at_block``) come from
      :func:`_build_substitutions`.
    * Section placeholders (``$top_section``, ``$shipped_section``,
      ``$promoted_section``) come from the matching ``render_*``
      helpers and are *empty strings* when the underlying input is
      empty (so the surrounding template can skip the section without
      leaking blank lines).
    * Unknown placeholders are left as-is via
      :meth:`string.Template.safe_substitute` — never raises.
    * A literal ``$`` in the template body is written as ``$$`` per
      stdlib :class:`string.Template` semantics.

    Raises
    ------
    TemplateError
        When the rendered output contains no ``\\n---\\n`` title/body
        separator. Operators editing the template typically delete
        this line by mistake; a clear error makes the breakage
        actionable rather than silent.
    """
    # Pre-render the multi-line sections so the template sees plain
    # strings and can position them freely. This is also where the
    # zero-count branch swaps in the legacy compact template — we
    # can't easily express "if unique_count == 0" inside a
    # string.Template, so we pick the right template body up-front.
    cases = [c for c in (auto_promoted_cases or []) if isinstance(c, dict)]
    top_section = render_top_section(scan_result, top_n)
    shipped_section = render_shipped_section(shipped_repos)
    promoted_section = render_promoted_section(cases)

    subs = _build_substitutions(
        scan_result=scan_result,
        shipped_repos=shipped_repos,
        auto_promoted_cases=cases,
        top_n=top_n,
        brand_prefix=brand_prefix,
        top_section=top_section,
        shipped_section=shipped_section,
        promoted_section=promoted_section,
    )

    # When the operator is using the bundled DEFAULT template *and*
    # there are zero unique candidates, swap to the compact zero-case
    # template so the resulting body matches the v0.3.0 wording
    # ("暂无可写热点 …") byte-for-byte. Custom templates always go
    # through the user's text — operators writing their own template
    # take responsibility for the zero-case copy.
    unique_count = int(scan_result.get("unique_count") or 0)
    effective_template = template
    if unique_count == 0 and template in (
        DEFAULT_LARK_SCAN_CARD_TPL,
        DEFAULT_TG_SCAN_CARD_TPL,
    ):
        effective_template = _ZERO_UNIQUE_TPL

    rendered = Template(effective_template).safe_substitute(subs)

    if _TITLE_BODY_SEPARATOR not in rendered:
        raise TemplateError(
            "scan card template missing title-body separator (expected "
            "a single '---' line between title and body)"
        )

    title, _, body_md = rendered.partition(_TITLE_BODY_SEPARATOR)
    return title.rstrip("\n"), body_md
