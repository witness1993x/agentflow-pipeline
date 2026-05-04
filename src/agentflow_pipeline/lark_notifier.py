"""Lark Custom Bot webhook notifier (stdlib-only port).

Outbound-only push notifier for the AgentFlow framework. Designed to be
the receiver of the ``agentflow-scan`` cron pipeline: once the daily scan
finishes, the operator gets a single Lark interactive card summarising
how many candidates were found, the top ``N`` hits, and which repos the
framework actually shipped that day.

This is a deliberate stdlib-only port of the production
``agentflow.shared.lark_webhook`` module — the framework runtime promises
zero third-party dependencies (PyYAML aside), so we use
``urllib.request`` rather than ``requests``. All other behaviour
(HMAC sign algorithm, ``HH:00 / HH:30`` deferral, custom-keyword
fallback, 19KB body cap, 5-req/sec soft floor) is byte-for-byte
compatible with the reference implementation so operators with an
existing ``LARK_WEBHOOK_*`` env block can reuse it as-is.

Configuration (env-driven, opt-in — empty URL = silent skip):

* ``LARK_WEBHOOK_URL`` — full webhook URL from Lark group bot setup.
  Empty/unset → all calls return ``sent=False, skipped_reason="no_url_configured"``.
* ``LARK_WEBHOOK_SECRET`` — optional. When set, every request includes
  ``timestamp`` + ``sign`` (HmacSHA256(timestamp + "\\n" + secret) → b64).
* ``LARK_WEBHOOK_KEYWORDS`` — optional comma-separated keywords. The
  module appends the first keyword to text bodies missing them so the
  bot's "自定义关键词" security setting doesn't drop posts.
* ``LARK_WEBHOOK_BRAND_PREFIX`` — optional title prefix (e.g. ``[AF]``).
* ``LARK_WEBHOOK_NO_DEFER`` — set to ``true`` to disable HH:00/HH:30
  deferral.
* ``LARK_WEBHOOK_DRY_RUN`` — set to ``true`` to skip the network call
  globally (returns a structured ``LarkSendResult`` plan).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable, TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest


_log = logging.getLogger("agentflow.lark_notifier")

# ---------------------------------------------------------------------------
# Tunables — kept identical to the reference implementation so existing ops
# runbooks stay valid.
# ---------------------------------------------------------------------------
_BODY_HARD_CAP_BYTES = 19_000
_DEFER_DODGE_SECONDS = 60   # how close to HH:00 / HH:30 counts as "rate-limit zone"
_DEFER_TARGET_OFFSET = 90   # how long to wait when in the zone
_MIN_INTERVAL_SECONDS = 0.22  # 5-req/sec soft floor (Lark hard cap is 5/s, 100/min)
_MAX_BUTTONS_PER_CARD = 5   # Lark interactive-card hard limit
_HTTP_TIMEOUT_SECONDS = 10

# Single-process serialization so back-to-back fan-outs don't blow the
# documented 5-per-second cap.
_SEND_LOCK = threading.Lock()
_last_send_at: float = 0.0


class LarkSendResult(TypedDict):
    """Structured outcome of a single send call.

    ``sent`` is the only authoritative success bit; the other fields are
    diagnostic. ``skipped_reason`` carries why we didn't actually POST
    (``"no_url_configured"``, ``"dry_run"``, ``"post_failed:<short>"``)
    or ``None`` when the send went through.
    """

    sent: bool
    skipped_reason: str | None
    card_title: str
    body_size_bytes: int


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str) -> bool:
    return _env_str(name).lower() == "true"


def _is_configured() -> bool:
    return bool(_env_str("LARK_WEBHOOK_URL"))


def _brand_prefix() -> str:
    raw = _env_str("LARK_WEBHOOK_BRAND_PREFIX")
    if not raw:
        return ""
    inner = raw.strip("[] ")
    return f"[{inner}] " if inner else ""


# ---------------------------------------------------------------------------
# Crypto + content guards
# ---------------------------------------------------------------------------
def _sign(timestamp: int, secret: str) -> str:
    """HmacSHA256(timestamp + "\\n" + secret) → b64.

    Per Lark Custom Bot spec: the body data passed to HMAC is empty;
    the secret used as KEY is the concatenation, and we b64-encode the
    digest. Byte-for-byte compatible with the reference implementation.
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _ensure_keyword(text: str, keywords: Iterable[str]) -> str:
    """Append the first keyword if none of the configured ones are present.

    Lark's "自定义关键词" security setting drops posts that lack any of the
    configured keywords with code 19024; this guard makes a misconfigured
    keyword set fail-safe rather than silently swallow the notification.
    """
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        return text
    if any(kw in text for kw in kws):
        return text
    return f"{text}\n\n[{kws[0]}]"


def _in_rate_limit_zone(now: datetime) -> bool:
    """``True`` when ``now`` is within ±60s of HH:00 or HH:30.

    Lark's docs explicitly call out 10:00 / 17:30 as common system-load
    spikes that produce ``11232`` errors; we generalize to any half-hour.
    """
    minute = now.minute
    second = now.second
    seconds_into_half = (minute % 30) * 60 + second
    distance_to_half = min(seconds_into_half, 30 * 60 - seconds_into_half)
    return distance_to_half <= _DEFER_DODGE_SECONDS


def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the longest text in ``payload`` so the JSON stays under the cap.

    Handles both text bodies (``payload["content"]["text"]``) and
    interactive-card bodies (``payload["card"]["elements"][i]["text"]["content"]``).
    Idempotent — re-called once if the first trim still over-runs.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) <= _BODY_HARD_CAP_BYTES:
        return payload
    cap_marker = "\n\n…(truncated for Lark 20K cap)"

    # Text-body path.
    if isinstance(payload.get("content"), dict):
        text = payload["content"].get("text")
        if isinstance(text, str) and len(text) > 200:
            keep = max(200, _BODY_HARD_CAP_BYTES // 2)
            payload["content"]["text"] = text[:keep] + cap_marker
            return _truncate(payload)

    # Interactive-card path: find the longest .text.content among elements.
    card = payload.get("card")
    if isinstance(card, dict):
        elements = card.get("elements")
        if isinstance(elements, list):
            longest_idx = -1
            longest_len = 0
            for i, el in enumerate(elements):
                if not isinstance(el, dict):
                    continue
                text_obj = el.get("text")
                if not isinstance(text_obj, dict):
                    continue
                content = text_obj.get("content")
                if isinstance(content, str) and len(content) > longest_len:
                    longest_len = len(content)
                    longest_idx = i
            if longest_idx >= 0 and longest_len > 200:
                keep = max(200, _BODY_HARD_CAP_BYTES // 2)
                content = elements[longest_idx]["text"]["content"]
                elements[longest_idx]["text"]["content"] = content[:keep] + cap_marker
                return _truncate(payload)
    return payload


# ---------------------------------------------------------------------------
# HTTP transport (stdlib)
# ---------------------------------------------------------------------------
def _http_post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST JSON to ``url`` via ``urllib.request``. Returns (status, body_json).

    Raises ``urlerror.URLError`` / ``OSError`` / ``ValueError`` on
    transport / decode failures; the caller is responsible for catching.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
        status = getattr(resp, "status", None) or resp.getcode()
        raw = resp.read().decode("utf-8", errors="replace")
    parsed: dict[str, Any] = {}
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                parsed = decoded
        except ValueError:
            parsed = {"_raw": raw[:200]}
    return int(status), parsed


def _post(payload: dict[str, Any], *, dry_run: bool) -> LarkSendResult:
    """Sign + size-guard + rate-limit-aware POST. Never raises.

    Returns a ``LarkSendResult`` describing what happened. ``card_title``
    is filled from the interactive-card header when present (otherwise
    ``""``); ``body_size_bytes`` is the final serialised JSON length so
    callers can log how close they came to the cap.
    """
    card_title = ""
    if payload.get("msg_type") == "interactive":
        try:
            card_title = (
                payload.get("card", {})
                .get("header", {})
                .get("title", {})
                .get("content", "")
            )
        except AttributeError:
            card_title = ""

    url = _env_str("LARK_WEBHOOK_URL")
    env_dry_run = _env_bool("LARK_WEBHOOK_DRY_RUN")
    effective_dry_run = bool(dry_run) or env_dry_run

    # Apply keyword fallback for text bodies before any sizing / signing,
    # mirroring the reference impl.
    keywords = (os.environ.get("LARK_WEBHOOK_KEYWORDS") or "").split(",")
    if payload.get("msg_type") == "text":
        text = payload.get("content", {}).get("text", "")
        payload["content"]["text"] = _ensure_keyword(text, keywords)

    if not url and not effective_dry_run:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return LarkSendResult(
            sent=False,
            skipped_reason="no_url_configured",
            card_title=card_title,
            body_size_bytes=size,
        )

    # Optional defer to dodge the integer-half-hour 11232 limit. Skipped
    # in dry_run so tests / CLI self-tests don't sleep.
    no_defer = _env_bool("LARK_WEBHOOK_NO_DEFER")
    if not no_defer and not effective_dry_run:
        now = datetime.now()
        if _in_rate_limit_zone(now):
            time.sleep(_DEFER_TARGET_OFFSET)

    secret = _env_str("LARK_WEBHOOK_SECRET")
    if secret:
        ts = int(time.time())
        payload = {**payload, "timestamp": str(ts), "sign": _sign(ts, secret)}

    payload = _truncate(payload)
    final_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    if effective_dry_run:
        return LarkSendResult(
            sent=False,
            skipped_reason="dry_run",
            card_title=card_title,
            body_size_bytes=final_size,
        )

    # Per-process interval guard (5 req/sec soft floor).
    global _last_send_at
    with _SEND_LOCK:
        elapsed = time.monotonic() - _last_send_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_send_at = time.monotonic()

    try:
        status, body = _http_post(url, payload)
    except urlerror.HTTPError as exc:
        _log.warning("Lark webhook HTTP %s: %s", exc.code, exc.reason)
        return LarkSendResult(
            sent=False,
            skipped_reason=f"post_failed:http_{exc.code}",
            card_title=card_title,
            body_size_bytes=final_size,
        )
    except urlerror.URLError as exc:
        _log.warning("Lark webhook URL error: %s", exc.reason)
        return LarkSendResult(
            sent=False,
            skipped_reason=f"post_failed:url_error:{exc.reason}",
            card_title=card_title,
            body_size_bytes=final_size,
        )
    except (TimeoutError, OSError, ValueError) as exc:
        _log.warning("Lark webhook send failed: %s", exc)
        return LarkSendResult(
            sent=False,
            skipped_reason=f"post_failed:{type(exc).__name__}:{exc}",
            card_title=card_title,
            body_size_bytes=final_size,
        )

    if status < 200 or status >= 300:
        _log.warning(
            "Lark webhook POST non-2xx: %s %s",
            status,
            json.dumps(body)[:200],
        )
        return LarkSendResult(
            sent=False,
            skipped_reason=f"post_failed:http_{status}",
            card_title=card_title,
            body_size_bytes=final_size,
        )

    code = body.get("code")
    if code not in (0, None):
        _log.warning(
            "Lark webhook returned code=%s msg=%s",
            code,
            body.get("msg"),
        )
        return LarkSendResult(
            sent=False,
            skipped_reason=f"post_failed:lark_code_{code}",
            card_title=card_title,
            body_size_bytes=final_size,
        )

    return LarkSendResult(
        sent=True,
        skipped_reason=None,
        card_title=card_title,
        body_size_bytes=final_size,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def send_text(text: str, *, dry_run: bool = False) -> LarkSendResult:
    """Plain text notification.

    Returns a ``LarkSendResult`` describing the outcome — never raises.
    No-ops with ``skipped_reason="no_url_configured"`` when
    ``LARK_WEBHOOK_URL`` is empty.
    """
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    return _post(payload, dry_run=dry_run)


def send_card(
    *,
    title: str,
    body_md: str,
    url_actions: list[tuple[str, str]] | None = None,
    accent: str = "blue",
    dry_run: bool = False,
) -> LarkSendResult:
    """Interactive card with optional URL buttons.

    ``url_actions`` is a list of (label, url) tuples. Lark Custom Bot
    only supports URL buttons — no callback. Empty / falsy entries are
    dropped silently so callers don't have to gate. The total button
    count is hard-capped at ``_MAX_BUTTONS_PER_CARD``.

    Prepends ``LARK_WEBHOOK_BRAND_PREFIX`` to the title when set.
    """
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"content": body_md, "tag": "lark_md"},
        }
    ]
    cleaned_actions = [
        (label, url) for label, url in (url_actions or []) if label and url
    ]
    if len(cleaned_actions) > _MAX_BUTTONS_PER_CARD:
        cleaned_actions = cleaned_actions[:_MAX_BUTTONS_PER_CARD]
    if cleaned_actions:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"content": label, "tag": "lark_md"},
                    "url": url,
                    "type": "default",
                }
                for label, url in cleaned_actions
            ],
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "content": _brand_prefix() + title,
                    "tag": "plain_text",
                },
                "template": accent,
            },
            "elements": elements,
        },
    }
    return _post(payload, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Convenience builder for the daily scan card.
# ---------------------------------------------------------------------------
def _format_scanned_at(scanned_at_raw: str) -> tuple[str, str]:
    """Parse ``scanned_at`` ISO string into (HH:MM local, full local ISO).

    Returns ("", scanned_at_raw) when the input doesn't parse cleanly so
    we always have something to display.
    """
    if not scanned_at_raw:
        return "", ""
    try:
        # Python's fromisoformat handles "+00:00" form natively from 3.11+.
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


def _summarise_by_source(by_source: dict[str, Any]) -> str:
    """Render ``by_source`` dict as ``"github=12, hn=4, reddit=0"``."""
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


def _format_top_line(idx: int, item: dict[str, Any]) -> str:
    """Render one ranked top hit as a single Markdown list line."""
    src = str(item.get("source") or "?")
    eng = item.get("engagement")
    if eng is None:
        # Fall back to the per-source raw scores when the aggregator hasn't
        # been run (e.g. caller passed raw GitHub items).
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


def notify_scan_complete(
    *,
    scan_result: dict,
    shipped_repos: list[dict],
    framework_repo_url: str = "https://github.com/witness1993x/agentflow-pipeline",
    top_n: int = 5,
    dry_run: bool = False,
    trends_view_url: str | None = None,
) -> LarkSendResult:
    """Render and send the daily-scan summary card.

    ``scan_result`` is the full output of ``scan_hotspots.run_scan()`` —
    we read ``scanned_at`` / ``by_source`` / ``unique_count`` /
    ``duplicates_merged`` / ``top``. ``shipped_repos`` is the list of
    repos the framework actually shipped this run, each item being
    ``{"name", "url", "language", "shape", "hotspot_id"}``.

    Buttons (in order, capped at 5):
      1. ``📚 framework repo``
      2. up to 3 ``⭐ <repo-name>`` buttons (sorted by ``hotspot_id``)
      3. ``📊 查看 scan.md`` when ``trends_view_url`` is set
    """
    unique_count = int(scan_result.get("unique_count") or 0)
    by_source = scan_result.get("by_source") or {}
    duplicates_merged = int(scan_result.get("duplicates_merged") or 0)
    scanned_at_raw = str(scan_result.get("scanned_at") or "")
    top_items = list(scan_result.get("top") or [])

    hh_mm, full_local = _format_scanned_at(scanned_at_raw)
    safe_top_n = max(1, int(top_n or 5))

    # Header / accent based on volume.
    if unique_count >= 30:
        accent = "green"
    elif unique_count >= 5:
        accent = "blue"
    elif unique_count == 0:
        accent = "grey"
    else:
        accent = "blue"

    title_suffix = f" ({hh_mm})" if hh_mm else ""
    title = f"🔎 AgentFlow · 每日热点扫描{title_suffix}"

    body_lines: list[str] = []
    if unique_count == 0:
        body_lines.append("**📊 今日扫描完成: 暂无可写热点** (上游空 / filter 过窄 / 配额耗尽)")
        body_lines.append("")
        body_lines.append(f"sources: {_summarise_by_source(by_source)}")
    else:
        body_lines.append(
            f"**📊 扫到 {unique_count} unique 候选** · sources: {_summarise_by_source(by_source)}"
        )
        if duplicates_merged:
            body_lines.append(f"_合并掉 {duplicates_merged} 条跨源重复_")
        body_lines.append("")
        body_lines.append(f"**🔥 Top {min(safe_top_n, len(top_items))}**:")
        for i, item in enumerate(top_items[:safe_top_n], 1):
            if not isinstance(item, dict):
                continue
            body_lines.append(_format_top_line(i, item))

    body_lines.append("")
    if shipped_repos:
        body_lines.append(f"**📦 Framework 已 ship ({len(shipped_repos)})**:")
        for repo in shipped_repos:
            if not isinstance(repo, dict):
                continue
            name = str(repo.get("name") or "?")
            lang = str(repo.get("language") or "?") or "?"
            shape = str(repo.get("shape") or "?") or "?"
            body_lines.append(f"- {name} ({lang}, {shape})")
    else:
        body_lines.append(
            "**📦 尚未 ship 任何 repo (cases/ 下无 final_status=publish 的案例)**"
        )

    if full_local:
        body_lines.append("")
        body_lines.append(f"scanned_at: `{full_local}`")

    body_md = "\n".join(body_lines)

    # Buttons: framework -> up to 3 ship'd repos -> trends-view link.
    actions: list[tuple[str, str]] = [
        ("📚 framework repo", framework_repo_url),
    ]
    sortable_repos = [r for r in shipped_repos if isinstance(r, dict)]
    sortable_repos.sort(key=lambda r: str(r.get("hotspot_id") or ""))
    for repo in sortable_repos[:3]:
        name = str(repo.get("name") or "")
        url = str(repo.get("url") or "")
        if name and url:
            actions.append((f"⭐ {name}", url))
    if trends_view_url:
        actions.append(("📊 查看 scan.md", trends_view_url))

    if len(actions) > _MAX_BUTTONS_PER_CARD:
        actions = actions[:_MAX_BUTTONS_PER_CARD]

    return send_card(
        title=title,
        body_md=body_md,
        url_actions=actions,
        accent=accent,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Self-test — `LARK_WEBHOOK_DRY_RUN=true python -m agentflow_pipeline.lark_notifier`
# ---------------------------------------------------------------------------
def _self_test() -> None:  # pragma: no cover - executed only via __main__
    """Render a fake daily-scan card in dry-run mode and print the plan.

    Performs zero network IO. Surfaces the resulting ``LarkSendResult``
    plus a head-of-payload preview so an operator can sanity-check the
    card structure without setting up a real Lark bot.
    """
    fake_scan = {
        "scanned_at": "2026-05-04T10:00:12+00:00",
        "by_source": {"github": 18, "hackernews": 7, "reddit": 5},
        "unique_count": 24,
        "duplicates_merged": 6,
        "top": [
            {
                "source": "github",
                "engagement": 745,
                "display_title": "sstklen/trump-code",
                "url": "https://github.com/sstklen/trump-code",
            },
            {
                "source": "hackernews",
                "engagement": 374,
                "title": "Show HN: tiny vector DB in 200 lines",
                "url": "https://news.ycombinator.com/item?id=1",
            },
            {
                "source": "reddit",
                "engagement": 188,
                "title": "DeFi flash loan tutorial",
                "url": "https://reddit.com/r/defi/comments/x",
            },
        ],
    }
    fake_shipped = [
        {
            "name": "chainstream-launch-radar",
            "url": "https://github.com/example/chainstream-launch-radar",
            "language": "TypeScript",
            "shape": "data_pipeline",
            "hotspot_id": "HSP-001",
        },
        {
            "name": "whale-pulse-evm",
            "url": "https://github.com/example/whale-pulse-evm",
            "language": "TypeScript",
            "shape": "data_pipeline",
            "hotspot_id": "HSP-002",
        },
    ]
    # Force dry-run regardless of env so the self-test never POSTs.
    os.environ.setdefault("LARK_WEBHOOK_DRY_RUN", "true")
    result = notify_scan_complete(
        scan_result=fake_scan,
        shipped_repos=fake_shipped,
        trends_view_url="https://example.com/trends/2026-05-04-10/scan.md",
        dry_run=True,
    )
    print("LarkSendResult:", json.dumps(result, ensure_ascii=False))
    print("--- card preview ---")
    print(f"title: {result['card_title']}")
    print(f"body_size_bytes: {result['body_size_bytes']}")
    print(f"skipped_reason: {result['skipped_reason']}")


if __name__ == "__main__":
    _self_test()
