"""Telegram Bot outbound notifier (stdlib-only port).

Outbound-only push notifier that mirrors :mod:`agentflow_pipeline.lark_notifier`
but talks the Telegram Bot HTTP API instead of a Lark Custom Bot webhook.
The framework runtime promises zero third-party dependencies (PyYAML aside),
so we use ``urllib.request`` rather than ``requests`` / ``python-telegram-bot``.

Configuration (env-driven, opt-in — empty token = silent skip):

* ``TELEGRAM_BOT_TOKEN`` — bot token from ``@BotFather``. Empty/unset →
  every call returns ``sent=False, skipped_reason="no_token_configured"``.
* ``TELEGRAM_CHAT_ID`` — default chat / group id used when ``chat_id`` is
  omitted at the call site. Empty/unset (and no kwarg override) →
  ``sent=False, skipped_reason="no_chat_id"``.
* ``TELEGRAM_DRY_RUN`` — set to ``true`` to skip the network call globally
  (returns a structured ``TgSendResult`` plan, no urlopen invocation).

Key differences vs :mod:`agentflow_pipeline.lark_notifier`:

* **Auth**: the token is part of the URL path (``/bot<TOKEN>/sendMessage``);
  no HMAC signing, no timestamp.
* **Buttons**: Telegram inline keyboards support both ``url`` AND
  ``callback_data`` buttons. ``callback_data`` payloads are capped at
  64 bytes by the Telegram API; we enforce that locally.
* **Body cap**: Telegram caps a single ``sendMessage`` at 4096 chars.
  Long bodies are split via :func:`_chunk_text`; only the FIRST chunk
  carries the ``reply_markup``.
* **Markdown**: Telegram's ``MarkdownV2`` requires escaping
  ``_*[]()~`>#+-=|{}.!`` and backslash. :func:`_escape_markdown_v2` is
  exposed for callers who emit interpolated MarkdownV2 bodies.
* **Rate limit**: Telegram's documented limit is 30 msg/s globally and
  1 msg/s per chat. We use a 100 ms soft inter-call gap, much more
  generous than the Lark 220 ms floor.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from typing import Any, TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest


_log = logging.getLogger("agentflow.tg_notifier")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
_TG_API_BASE = "https://api.telegram.org"
_HTTP_TIMEOUT_SECONDS = 30
_MIN_INTERVAL_SECONDS = 0.10  # 10 msg/sec soft cap (TG global is 30/sec)
_TG_MAX_MESSAGE_CHARS = 4096
_TG_CHUNK_SAFE_CHARS = 4000  # leave headroom for parse-mode glitches
_TG_CALLBACK_DATA_MAX_BYTES = 64
_TG_MAX_BUTTONS_PER_ROW = 3
_TG_MAX_BUTTONS_TOTAL = 8  # mirrors Lark cap-of-5 spirit but a touch looser
_TG_BUTTON_OVERFLOW_NOTE_BYTES = 12

# Single-process serialization so back-to-back fan-outs don't blow the
# documented 30-per-second cap.
_SEND_LOCK = threading.Lock()
_last_send_at: float = 0.0


class TgSendResult(TypedDict):
    """Structured outcome of a single Telegram send.

    ``sent`` is the only authoritative success bit; the other fields are
    diagnostic. ``skipped_reason`` is one of:

    * ``"no_token_configured"`` — ``TELEGRAM_BOT_TOKEN`` empty/unset.
    * ``"no_chat_id"`` — neither kwarg nor ``TELEGRAM_CHAT_ID`` resolves.
    * ``"dry_run"`` — caller (or env ``TELEGRAM_DRY_RUN=true``) suppressed
      the network call.
    * ``"post_failed:<short>"`` — transport / API error (URL error,
      timeout, ``ok=false`` body, non-2xx HTTP).
    * ``None`` — actual ``sendMessage`` succeeded.

    For multi-chunk sends (body > 4096 chars), ``message_id`` is the id
    of the *first* chunk (the only one carrying ``reply_markup``).
    """

    sent: bool
    skipped_reason: str | None
    message_id: int | None
    chat_id: int | str | None
    body_size_chars: int


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str) -> bool:
    return _env_str(name).lower() == "true"


def _ensure_token() -> str | None:
    """Return the bot token from env, or ``None`` when missing.

    Never raises so call sites can fail-quiet via ``TgSendResult``.
    """
    token = _env_str("TELEGRAM_BOT_TOKEN")
    return token or None


def _resolve_chat_id(chat_id: int | str | None) -> int | str | None:
    """Pick the explicit kwarg if set, else fall back to env."""
    if chat_id is not None and str(chat_id).strip() != "":
        return chat_id
    env_val = _env_str("TELEGRAM_CHAT_ID")
    if not env_val:
        return None
    # Telegram chat ids are typically negative integers for groups; preserve
    # int form when parseable so the wire payload matches manual usage.
    try:
        return int(env_val)
    except ValueError:
        return env_val


# ---------------------------------------------------------------------------
# MarkdownV2 + chunking helpers
# ---------------------------------------------------------------------------
# Per https://core.telegram.org/bots/api#markdownv2-style — these are the
# characters that MUST be escaped with a leading backslash anywhere in the
# message body (outside of pre/code blocks).
_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def _escape_markdown_v2(text: str) -> str:
    """Backslash-escape every MarkdownV2-special char in ``text``.

    Use this on user-provided / dynamic strings before splicing them into
    a body that uses ``parse_mode='MarkdownV2'``. Idempotent only when
    ``text`` contains no pre-escaped specials, which is intentional —
    callers that already escape should not pass through this helper.
    """
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ch in _MARKDOWN_V2_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _chunk_text(text: str, *, max_size: int = _TG_CHUNK_SAFE_CHARS) -> list[str]:
    """Split ``text`` into chunks <= ``max_size`` chars.

    Prefers paragraph (``\\n\\n``) boundaries, then sentence terminators
    (Chinese + Latin), then a hard char cut as last resort. Returns a
    single-element list when the input fits.

    Mirrors the production ``tg_client._chunk_text`` algorithm so
    operators see the same wrapping behavior across the two codebases.
    """
    text = text or ""
    if len(text) <= max_size:
        return [text] if text else []
    chunks: list[str] = []
    remainder = text
    while remainder:
        if len(remainder) <= max_size:
            chunks.append(remainder)
            break
        head = remainder[:max_size]
        # Prefer splitting at the last paragraph break inside head.
        cut = head.rfind("\n\n")
        if cut < max_size // 2:
            cut = -1
        if cut == -1:
            for term in ("。", "！", "？", ".", "!", "?", "\n"):
                idx = head.rfind(term)
                if idx >= max_size // 2:
                    cut = idx + len(term)
                    break
        if cut == -1:
            cut = max_size  # hard cut
        chunks.append(remainder[:cut].rstrip())
        remainder = remainder[cut:].lstrip()
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Inline keyboard builder
# ---------------------------------------------------------------------------
def _truncate_callback_data(callback_data: str) -> str:
    """Truncate ``callback_data`` to the 64-byte Telegram limit.

    Telegram silently drops button presses whose ``callback_data`` exceeds
    64 bytes (UTF-8); we truncate at the byte boundary to keep the rest of
    the keyboard usable. Caller-supplied IDs that are at risk of being
    cut should pre-shorten — we log a warning when truncation actually
    happens.
    """
    if not callback_data:
        return ""
    encoded = callback_data.encode("utf-8")
    if len(encoded) <= _TG_CALLBACK_DATA_MAX_BYTES:
        return callback_data
    _log.warning(
        "tg_notifier: callback_data over %d bytes (%d), truncating",
        _TG_CALLBACK_DATA_MAX_BYTES,
        len(encoded),
    )
    truncated = encoded[:_TG_CALLBACK_DATA_MAX_BYTES]
    # Decode back, dropping any partial multi-byte tail.
    return truncated.decode("utf-8", errors="ignore")


def _build_inline_keyboard(
    *,
    url_actions: list[tuple[str, str]] | None,
    callback_actions: list[tuple[str, str]] | None,
    max_total: int = _TG_MAX_BUTTONS_TOTAL,
    row_size: int = _TG_MAX_BUTTONS_PER_ROW,
) -> dict[str, Any] | None:
    """Build a ``reply_markup={"inline_keyboard": ...}`` payload.

    Drops empty/falsy entries silently. URL buttons come first, then
    callback buttons (so the destructive ``callback_data`` actions are
    visually below the navigational links). Returns ``None`` when nothing
    survives filtering so the caller can omit ``reply_markup`` entirely.
    """
    buttons: list[dict[str, Any]] = []
    for label, url in (url_actions or []):
        if label and url:
            buttons.append({"text": str(label), "url": str(url)})
    for label, data in (callback_actions or []):
        if label and data:
            buttons.append({
                "text": str(label),
                "callback_data": _truncate_callback_data(str(data)),
            })
    if not buttons:
        return None
    if len(buttons) > max_total:
        buttons = buttons[:max_total]
    rows: list[list[dict[str, Any]]] = []
    for i in range(0, len(buttons), row_size):
        rows.append(buttons[i : i + row_size])
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# HTTP transport (stdlib)
# ---------------------------------------------------------------------------
def _api_url(token: str, method: str) -> str:
    return f"{_TG_API_BASE}/bot{token}/{method}"


def _http_post(
    token: str,
    method: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """POST JSON to the Telegram bot API. Returns (status, body_json).

    Raises ``urlerror.URLError`` / ``OSError`` / ``ValueError`` on
    transport / decode failures; the caller is responsible for catching.
    The token never appears in logs or exception text — only the path
    suffix (``/sendMessage`` etc) is referenced in error messages.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        _api_url(token, method),
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


def _post(
    method: str,
    payload: dict[str, Any],
    *,
    token: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Inter-call rate-limited POST.

    Returns ``(ok, skipped_reason, result_dict)``. ``ok=True`` only when
    the API returned ``ok: true``; the result dict is the ``result`` field
    on success, the parsed body otherwise.
    """
    global _last_send_at
    with _SEND_LOCK:
        elapsed = time.monotonic() - _last_send_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_send_at = time.monotonic()

    try:
        status, body = _http_post(token, method, payload)
    except urlerror.HTTPError as exc:
        _log.warning("Telegram %s HTTP %s: %s", method, exc.code, exc.reason)
        return False, f"post_failed:http_{exc.code}", {}
    except urlerror.URLError as exc:
        _log.warning("Telegram %s URL error: %s", method, exc.reason)
        return False, f"post_failed:url_error:{exc.reason}", {}
    except (TimeoutError, OSError, ValueError) as exc:
        _log.warning("Telegram %s send failed: %s", method, exc)
        return False, f"post_failed:{type(exc).__name__}:{exc}", {}

    if status < 200 or status >= 300:
        _log.warning(
            "Telegram %s non-2xx: %s %s",
            method,
            status,
            json.dumps(body)[:200],
        )
        return False, f"post_failed:http_{status}", body

    if not body.get("ok"):
        desc = str(body.get("description") or "unknown_error")
        code = body.get("error_code")
        _log.warning(
            "Telegram %s returned ok=false: %s (code=%s)", method, desc, code,
        )
        # Strip control / spaces from desc so the reason string round-trips
        # through logs cleanly.
        short_desc = desc.replace("\n", " ").strip()[:120]
        return False, f"post_failed:tg_{code}:{short_desc}", body

    result = body.get("result") or {}
    if not isinstance(result, dict):
        result = {"_result": result}
    return True, None, result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def send_text(
    text: str,
    *,
    chat_id: int | str | None = None,
    parse_mode: str | None = "MarkdownV2",
    disable_web_page_preview: bool = True,
    dry_run: bool = False,
) -> TgSendResult:
    """Send a plain Telegram text message.

    Returns a :class:`TgSendResult` describing the outcome — never raises.
    No-ops with ``skipped_reason="no_token_configured"`` when the bot
    token is unset and with ``"no_chat_id"`` when neither the kwarg nor
    ``TELEGRAM_CHAT_ID`` resolves.

    Bodies over 4096 chars are auto-chunked; ``message_id`` reports the
    id of the first chunk only.
    """
    body_size = len(text or "")
    resolved_chat = _resolve_chat_id(chat_id)
    env_dry_run = _env_bool("TELEGRAM_DRY_RUN")
    effective_dry_run = bool(dry_run) or env_dry_run

    token = _ensure_token()
    if not token and not effective_dry_run:
        return TgSendResult(
            sent=False,
            skipped_reason="no_token_configured",
            message_id=None,
            chat_id=resolved_chat,
            body_size_chars=body_size,
        )
    if resolved_chat is None and not effective_dry_run:
        return TgSendResult(
            sent=False,
            skipped_reason="no_chat_id",
            message_id=None,
            chat_id=None,
            body_size_chars=body_size,
        )
    if effective_dry_run:
        return TgSendResult(
            sent=False,
            skipped_reason="dry_run",
            message_id=None,
            chat_id=resolved_chat,
            body_size_chars=body_size,
        )

    return _send_text_chunks(
        token=token or "",
        chat_id=resolved_chat,
        text=text or "",
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
        reply_markup=None,
    )


def _send_text_chunks(
    *,
    token: str,
    chat_id: int | str | None,
    text: str,
    parse_mode: str | None,
    disable_web_page_preview: bool,
    reply_markup: dict[str, Any] | None,
) -> TgSendResult:
    """Internal: split + send long texts. ``reply_markup`` only on chunk #1."""
    chunks = _chunk_text(text, max_size=_TG_CHUNK_SAFE_CHARS)
    if not chunks:
        chunks = [""]
    body_size = len(text)
    first_message_id: int | None = None
    last_skipped: str | None = None

    for idx, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if idx == 0 and reply_markup is not None:
            payload["reply_markup"] = reply_markup

        ok, reason, result = _post("sendMessage", payload, token=token)
        if not ok:
            return TgSendResult(
                sent=False,
                skipped_reason=reason,
                message_id=first_message_id,
                chat_id=chat_id,
                body_size_chars=body_size,
            )
        if idx == 0:
            mid = result.get("message_id")
            try:
                first_message_id = int(mid) if mid is not None else None
            except (TypeError, ValueError):
                first_message_id = None
        last_skipped = None  # noqa: F841 — placeholder for future per-chunk diag

    return TgSendResult(
        sent=True,
        skipped_reason=None,
        message_id=first_message_id,
        chat_id=chat_id,
        body_size_chars=body_size,
    )


def send_card(
    *,
    title: str = "",
    body_md: str,
    url_actions: list[tuple[str, str]] | None = None,
    callback_actions: list[tuple[str, str]] | None = None,
    chat_id: int | str | None = None,
    parse_mode: str | None = "MarkdownV2",
    disable_web_page_preview: bool = True,
    dry_run: bool = False,
) -> TgSendResult:
    """Send an inline-keyboard "card" message.

    Telegram has no native "card" container — we fake it by prepending
    ``*<escaped title>*`` to ``body_md`` (when ``title`` is non-empty)
    and attaching a ``reply_markup={"inline_keyboard": ...}`` built from
    ``url_actions`` + ``callback_actions``.

    ``callback_actions`` entries whose ``callback_data`` exceed 64 bytes
    are truncated locally to satisfy the Telegram limit (a warning is
    logged). The combined button count is capped at 8, laid out in rows
    of up to 3.

    For body > 4096 chars the message is split via :func:`_chunk_text` and
    only the FIRST chunk carries the keyboard; subsequent chunks are
    plain continuations.
    """
    if title:
        title_line = f"*{_escape_markdown_v2(title)}*"
        full_body = f"{title_line}\n\n{body_md}" if body_md else title_line
    else:
        full_body = body_md

    body_size = len(full_body)
    resolved_chat = _resolve_chat_id(chat_id)
    env_dry_run = _env_bool("TELEGRAM_DRY_RUN")
    effective_dry_run = bool(dry_run) or env_dry_run

    token = _ensure_token()
    if not token and not effective_dry_run:
        return TgSendResult(
            sent=False,
            skipped_reason="no_token_configured",
            message_id=None,
            chat_id=resolved_chat,
            body_size_chars=body_size,
        )
    if resolved_chat is None and not effective_dry_run:
        return TgSendResult(
            sent=False,
            skipped_reason="no_chat_id",
            message_id=None,
            chat_id=None,
            body_size_chars=body_size,
        )

    reply_markup = _build_inline_keyboard(
        url_actions=url_actions,
        callback_actions=callback_actions,
    )

    if effective_dry_run:
        return TgSendResult(
            sent=False,
            skipped_reason="dry_run",
            message_id=None,
            chat_id=resolved_chat,
            body_size_chars=body_size,
        )

    return _send_text_chunks(
        token=token or "",
        chat_id=resolved_chat,
        text=full_body,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
        reply_markup=reply_markup,
    )


# ---------------------------------------------------------------------------
# Convenience builder for the daily scan card.
# ---------------------------------------------------------------------------
def _build_url_actions_for_tg(
    *,
    framework_repo_url: str,
    shipped_repos: list[dict],
    promoted_cases: list[dict],
    trends_view_url: str | None,
    max_total: int = 5,
) -> list[tuple[str, str]]:
    """Mirror the Lark notify_scan_complete button order for Telegram.

    Order:
      1. ``framework repo`` link
      2. up to 3 ``<repo-name>`` links (sorted by ``hotspot_id``)
      3. ``Git case [N]`` link (first case's ``source_url``) when present
      4. ``查看 scan.md`` link when ``trends_view_url`` is set

    Total capped at ``max_total`` (default 5) so the keyboard stays
    visually compact on mobile clients.
    """
    actions: list[tuple[str, str]] = [
        (f"framework repo", framework_repo_url),
    ]
    sortable_repos = [r for r in shipped_repos if isinstance(r, dict)]
    sortable_repos.sort(key=lambda r: str(r.get("hotspot_id") or ""))
    for repo in sortable_repos[:3]:
        name = str(repo.get("name") or "")
        url = str(repo.get("url") or "")
        if name and url:
            actions.append((f"⭐ {name}", url))
    if promoted_cases:
        first_source = str(promoted_cases[0].get("source_url") or "")
        if first_source:
            actions.append(
                (f"🧭 Git case [{len(promoted_cases)}]", first_source)
            )
    if trends_view_url:
        actions.append(("scan.md", trends_view_url))
    if len(actions) > max_total:
        actions = actions[:max_total]
    return actions


def _build_callback_actions_for_tg(
    *,
    promoted_cases: list[dict],
) -> list[tuple[str, str]]:
    """Build Git-case callback buttons for the first promoted case.

    The callback namespace is deliberately ``case:*`` so it cannot collide with
    the article package's ``A/B/C/D:*`` Gate vocabulary.
    """
    if not promoted_cases:
        return []
    hotspot_id = str(promoted_cases[0].get("hotspot_id") or "").strip()
    if not hotspot_id:
        return []
    return [
        ("✅ 8 gates", f"case:dry-publish:{hotspot_id}"),
        ("🧱 write stub", f"case:write-stub:{hotspot_id}"),
        ("😴 snooze 7d", f"case:snooze:{hotspot_id}:7d"),
        ("🗑 drop", f"case:drop:{hotspot_id}"),
    ]


def notify_scan_complete(
    *,
    scan_result: dict,
    shipped_repos: list[dict],
    framework_repo_url: str = "https://github.com/witness1993x/agentflow-pipeline",
    top_n: int = 5,
    dry_run: bool = False,
    trends_view_url: str | None = None,
    auto_promoted_cases: list[dict] | None = None,
    chat_id: int | str | None = None,
) -> TgSendResult:
    """Render and send the daily-scan summary card via Telegram.

    Body content is delegated to
    ``agentflow_pipeline.notification_templates.render_scan_card`` (with
    the ``tg_scan_card`` template) so the Telegram and Lark cards stay
    in lockstep visually. The template module is imported lazily so this
    file remains importable in environments where it hasn't been
    deployed yet (e.g. partial rollouts).
    """
    promoted_cases = [
        c for c in (auto_promoted_cases or []) if isinstance(c, dict)
    ]

    # Lazy import: notification_templates is built by L2 and may not be
    # present in every checkout yet.
    from agentflow_pipeline import notification_templates as _nt

    template = _nt.resolve_template(name="tg_scan_card")
    title, body_md = _nt.render_scan_card(
        template=template,
        scan_result=scan_result,
        shipped_repos=shipped_repos,
        auto_promoted_cases=promoted_cases,
        top_n=top_n,
    )

    url_actions = _build_url_actions_for_tg(
        framework_repo_url=framework_repo_url,
        shipped_repos=shipped_repos,
        promoted_cases=promoted_cases,
        trends_view_url=trends_view_url,
    )
    callback_actions = _build_callback_actions_for_tg(promoted_cases=promoted_cases)

    return send_card(
        title=title,
        body_md=body_md,
        url_actions=url_actions,
        callback_actions=callback_actions,
        chat_id=chat_id,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Console-script entry point: ``agentflow-tg-notify``
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentflow-tg-notify",
        description=(
            "Send a Telegram message via the AgentFlow tg_notifier "
            "(stdlib-only). Used for hand-testing the wiring + cron jobs."
        ),
    )
    body_grp = p.add_mutually_exclusive_group()
    body_grp.add_argument("--text", help="message body (plain MarkdownV2)")
    body_grp.add_argument(
        "--from-stdin",
        action="store_true",
        help="read body from STDIN (overrides --text)",
    )
    p.add_argument(
        "--card-title",
        help="treat input as a card; prepended as bold title line",
    )
    p.add_argument(
        "--card-body",
        help="card body (alternative to --text when using --card-title)",
    )
    p.add_argument(
        "--callback-data",
        action="append",
        default=[],
        metavar="LABEL=DATA",
        help=(
            "add an inline-keyboard callback button. "
            "Repeatable. Example: --callback-data 'Approve=case:approve:HSP-1'"
        ),
    )
    p.add_argument(
        "--url-action",
        action="append",
        default=[],
        metavar="LABEL=URL",
        help="add an inline-keyboard URL button. Repeatable.",
    )
    p.add_argument(
        "--chat-id",
        help="override TELEGRAM_CHAT_ID for this call",
    )
    p.add_argument(
        "--parse-mode",
        default="MarkdownV2",
        help="parse_mode override (default: MarkdownV2). Pass empty to disable.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="don't actually POST; print the planned TgSendResult",
    )
    return p


def _parse_label_data_pairs(
    raw: list[str], *, kind: str,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in raw or []:
        if "=" not in item:
            print(
                f"agentflow-tg-notify: bad --{kind} (expect LABEL=VALUE): "
                f"{item!r}",
                file=sys.stderr,
            )
            continue
        label, _, value = item.partition("=")
        out.append((label.strip(), value.strip()))
    return out


def _main_entry(argv: list[str] | None = None) -> int:
    """Console-script body for ``agentflow-tg-notify``.

    Returns a process exit code (``0`` on success / clean dry-run, ``1``
    when the result reports ``sent=False`` outside of dry-run / not
    configured cases).
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Resolve message body.
    if args.from_stdin:
        text = sys.stdin.read()
    elif args.card_body is not None:
        text = args.card_body
    else:
        text = args.text or ""

    callback_actions = _parse_label_data_pairs(
        args.callback_data, kind="callback-data",
    )
    url_actions = _parse_label_data_pairs(args.url_action, kind="url-action")
    parse_mode = args.parse_mode if args.parse_mode != "" else None

    if args.card_title is not None:
        result = send_card(
            title=args.card_title,
            body_md=text,
            url_actions=url_actions or None,
            callback_actions=callback_actions or None,
            chat_id=args.chat_id,
            parse_mode=parse_mode,
            dry_run=args.dry_run,
        )
    else:
        # Plain text + optional buttons via send_card with empty title.
        if url_actions or callback_actions:
            result = send_card(
                title="",
                body_md=text,
                url_actions=url_actions or None,
                callback_actions=callback_actions or None,
                chat_id=args.chat_id,
                parse_mode=parse_mode,
                dry_run=args.dry_run,
            )
        else:
            result = send_text(
                text,
                chat_id=args.chat_id,
                parse_mode=parse_mode,
                dry_run=args.dry_run,
            )

    print(json.dumps(result, ensure_ascii=False))
    if result["sent"]:
        return 0
    if result["skipped_reason"] in {"dry_run", "no_token_configured", "no_chat_id"}:
        return 0
    return 1


# ---------------------------------------------------------------------------
# Self-test — `TELEGRAM_DRY_RUN=true python -m agentflow_pipeline.tg_notifier`
# ---------------------------------------------------------------------------
def _self_test() -> None:  # pragma: no cover - executed only via __main__
    """Print a planned payload in dry-run mode (no network IO)."""
    os.environ.setdefault("TELEGRAM_DRY_RUN", "true")
    result = send_card(
        title="AgentFlow self-test",
        body_md="Hello from `tg_notifier`.",
        url_actions=[("repo", "https://github.com/witness1993x/agentflow-pipeline")],
        callback_actions=[("ack", "selftest:ack")],
        dry_run=True,
    )
    print("TgSendResult:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    _self_test()
