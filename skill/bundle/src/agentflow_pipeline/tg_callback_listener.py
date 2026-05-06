"""Telegram callback long-poll daemon for AgentFlow case actions.

Outbound notifications (``tg_notifier``) emit interactive cards with
``inline_keyboard`` buttons whose ``callback_data`` encodes a target
case action (e.g. ``case:dry-publish:HSP-005``). When the operator
taps a button TG queues a ``callback_query`` update against the bot;
this module is the long-poll daemon that drains that queue and turns
each click into a real framework action via
``case_actions.dispatch_callback_action``.

Design contract:

* Pure stdlib (``urllib`` + ``json`` + ``time`` + ``threading`` +
  ``signal``); no third-party deps so the framework stays
  embeddable in any host project.
* Single-process loop: ``getUpdates`` long-polls TG (default 25s),
  filters to ``callback_query`` updates plus Lark deep-link ``/start``
  messages, applies auth + rate-limiting, and dispatches to a pluggable handler. Every
  failure path falls through ``answer_callback_query`` so the user
  always gets a toast — silent dropping is the worst outcome.
* SIGTERM / SIGINT cleanly stop the loop and flush the rolling
  ``ListenerStats`` blob to ``<host_root>/trends/_listener.stats.json``
  for ops dashboards.

Auth model (high → low trust):

1. ``allowed_chat_ids`` whitelist (``callback_query.from.id`` /
   ``callback_query.message.chat.id`` either match) — first defence,
   blocks any chat the bot was uninvitedly added to.
2. ``callback_secret`` prefix on every ``callback_data`` payload —
   defence-in-depth against an attacker who manages to share a bot
   with the operator and tries to forge ``callback_data`` strings.
   The sender (``tg_notifier``) prepends ``<secret>:`` when emitting
   the keyboard; this listener strips the prefix before dispatch.
3. Per-chat sliding-window rate-limit (default 10 actions / minute).

If ``callback_secret`` is unset the daemon prints a stderr warning
on startup but doesn't refuse — useful for single-operator dev
setups where the whitelist alone is enough. Production should
always set both.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, TypedDict
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


_log = logging.getLogger("agentflow.tg_callback_listener")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT_OVERHEAD = 10  # extra seconds on top of long-poll timeout
_DEFAULT_LONG_POLL_TIMEOUT = 25
_DEFAULT_MAX_ACTIONS_PER_MINUTE = 10
_RATE_LIMIT_WINDOW_SECONDS = 60
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 30.0
_ERROR_HISTORY_CAP = 10
_CALLBACK_DATA_HARD_LIMIT = 64  # TG protocol limit; sender enforces, we log


class ListenerStats(TypedDict):
    """Rolling counters surfaced via ``--stats`` and the disk blob.

    All times are ISO-8601 UTC strings. ``errors`` is a bounded list
    (last ``_ERROR_HISTORY_CAP`` entries) so the blob never grows
    unbounded; old errors are silently dropped.
    """

    started_at: str
    polls: int
    callback_queries_received: int
    actions_dispatched: int
    last_poll_at: str | None
    last_action_at: str | None
    errors: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stats_path(host_root: Path) -> Path:
    return host_root / "trends" / "_listener.stats.json"


def _default_dispatcher(callback_data: str, context: dict[str, Any]) -> dict[str, Any]:
    """Lazy-import default dispatcher → ``case_actions.dispatch_callback_action``.

    Lazy so this module imports cleanly even before L5 lands the
    ``case_actions`` sibling — tests inject their own dispatcher anyway.
    """
    from .case_actions import dispatch_callback_action  # type: ignore[import-not-found]

    root = context["root"]
    user_id = context.get("user_id", "unknown")
    return dispatch_callback_action(
        callback_data,
        root=root,
        actor=f"tg:{user_id}",
    )


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------
class TgCallbackListener:
    """Long-poll daemon translating TG button taps into framework actions.

    See module docstring for the full contract. The class is
    intentionally constructor-injected (bot_token, host_root,
    optional ACL knobs, optional dispatcher) so tests can build a
    fully-isolated instance without touching env vars.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        host_root: Path,
        allowed_chat_ids: list[int | str] | None = None,
        callback_secret: str | None = None,
        long_poll_timeout: int = _DEFAULT_LONG_POLL_TIMEOUT,
        max_actions_per_minute: int = _DEFAULT_MAX_ACTIONS_PER_MINUTE,
        action_dispatcher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        self._bot_token = bot_token
        self._host_root = Path(host_root)
        self._allowed_chat_ids = (
            {str(c) for c in allowed_chat_ids} if allowed_chat_ids else None
        )
        self._callback_secret = callback_secret or None
        self._long_poll_timeout = max(1, int(long_poll_timeout))
        self._max_actions_per_minute = max(1, int(max_actions_per_minute))
        self._dispatcher = action_dispatcher or _default_dispatcher

        self._offset: int = 0
        self._stop_event = threading.Event()
        self._rate_buckets: dict[str, Deque[float]] = {}
        self._stats: ListenerStats = {
            "started_at": _utc_now_iso(),
            "polls": 0,
            "callback_queries_received": 0,
            "actions_dispatched": 0,
            "last_poll_at": None,
            "last_action_at": None,
            "errors": [],
        }

        if not self._callback_secret:
            print(
                "[tg-listener] WARN callback_secret not configured; ANY chat with bot can trigger actions",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    @property
    def stats(self) -> ListenerStats:
        # Defensive copy — callers shouldn't be able to mutate the
        # rolling counters by accident (e.g. ``listener.stats["polls"] = 0``).
        return {
            "started_at": self._stats["started_at"],
            "polls": self._stats["polls"],
            "callback_queries_received": self._stats["callback_queries_received"],
            "actions_dispatched": self._stats["actions_dispatched"],
            "last_poll_at": self._stats["last_poll_at"],
            "last_action_at": self._stats["last_action_at"],
            "errors": list(self._stats["errors"]),
        }

    def stop(self) -> None:
        """Request a clean shutdown. Safe to call from a signal handler."""
        self._stop_event.set()

    def run_once(self) -> int:
        """Single ``getUpdates`` cycle. Returns count of dispatched actions.

        Filtering / auth / rate-limit failures still consume the
        update (i.e. advance ``offset``) but don't count toward the
        return value — only successful dispatch attempts do.
        """
        self._stats["polls"] += 1
        self._stats["last_poll_at"] = _utc_now_iso()

        try:
            updates = self._get_updates()
        except (urlerror.URLError, OSError, ValueError) as exc:
            self._record_error(f"get_updates_failed:{type(exc).__name__}:{exc}")
            raise

        dispatched = 0
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Always advance the offset, even for non-callback updates,
                # so we don't replay them next poll.
                self._offset = max(self._offset, update_id + 1)
            cb = update.get("callback_query")
            if isinstance(cb, dict):
                self._stats["callback_queries_received"] += 1
                try:
                    if self._handle_callback_query(cb):
                        dispatched += 1
                except Exception as exc:  # noqa: BLE001 - daemon must not die
                    self._record_error(
                        f"handle_callback_failed:{type(exc).__name__}:{exc}"
                    )
                    cb_id = cb.get("id")
                    if cb_id:
                        self._safe_answer(cb_id, text="Internal error")
                continue

            msg = update.get("message")
            if isinstance(msg, dict):
                try:
                    if self._handle_start_message(msg):
                        dispatched += 1
                except Exception as exc:  # noqa: BLE001 - daemon must not die
                    self._record_error(
                        f"handle_start_failed:{type(exc).__name__}:{exc}"
                    )
                    chat = msg.get("chat") or {}
                    self._safe_send_message(chat.get("id"), "Internal error")

        return dispatched

    def run_forever(self) -> None:
        """Blocking long-poll loop. Returns on SIGTERM / SIGINT.

        Installs signal handlers for the running thread (when called
        from main) and exits cleanly via the internal stop event.
        Errors during a poll cycle trigger an exponential backoff
        capped at ``_BACKOFF_MAX`` so a flapping network can't tight-loop.
        """
        self._install_signal_handlers()
        backoff = _BACKOFF_MIN
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                    backoff = _BACKOFF_MIN  # reset on a successful poll
                except (urlerror.URLError, OSError, ValueError):
                    # Already recorded into stats by run_once; back off and retry.
                    if self._stop_event.wait(timeout=backoff):
                        break
                    backoff = min(_BACKOFF_MAX, backoff * 2)
                    continue
                except Exception as exc:  # noqa: BLE001 - last-resort guard
                    self._record_error(
                        f"loop_unexpected:{type(exc).__name__}:{exc}"
                    )
                    if self._stop_event.wait(timeout=backoff):
                        break
                    backoff = min(_BACKOFF_MAX, backoff * 2)
                    continue
        finally:
            self._flush_stats_to_disk()

    # ------------------------------------------------------------------
    # Internals — TG transport
    # ------------------------------------------------------------------
    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._bot_token}/{method}"

    def _http_post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self._api_url(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # ``getUpdates`` blocks server-side for ``timeout`` seconds; add a
        # small client-side overhead so we don't trip our own urlopen
        # timeout before the server replies.
        timeout = self._long_poll_timeout + _HTTP_TIMEOUT_OVERHEAD
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, dict) else {}
        except ValueError:
            return {}

    def _get_updates(self) -> list[dict[str, Any]]:
        payload = {
            "offset": self._offset,
            "timeout": self._long_poll_timeout,
            "allowed_updates": ["callback_query", "message"],
        }
        body = self._http_post("getUpdates", payload)
        if not body.get("ok"):
            return []
        result = body.get("result")
        return result if isinstance(result, list) else []

    def _safe_answer(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        """``answerCallbackQuery`` that swallows transport errors.

        Always best-effort — failing here would only spam stats and
        the user already saw nothing happen. We log and move on so
        the next poll cycle can still drain the queue.
        """
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        try:
            self._http_post("answerCallbackQuery", payload)
        except (urlerror.URLError, OSError, ValueError) as exc:
            self._record_error(f"answer_failed:{type(exc).__name__}:{exc}")

    def _safe_send_message(
        self,
        chat_id: Any,
        text: str,
        *,
        reply_to_message_id: Any = None,
    ) -> None:
        """``sendMessage`` that swallows transport errors for /start flows."""
        if chat_id is None:
            return
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096] if text else "Done",
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        try:
            self._http_post("sendMessage", payload)
        except (urlerror.URLError, OSError, ValueError) as exc:
            self._record_error(f"send_message_failed:{type(exc).__name__}:{exc}")

    def _safe_remove_buttons(self, chat_id: Any, message_id: Any) -> None:
        """Strip the inline keyboard from the originating message.

        Called after a successful dispatch so the operator can't
        click the same button twice (which would re-run the action).
        Best-effort: a 4xx response from TG (message too old, deleted,
        etc.) is recorded but never crashes the daemon.
        """
        if chat_id is None or message_id is None:
            return
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        }
        try:
            self._http_post("editMessageReplyMarkup", payload)
        except (urlerror.URLError, OSError, ValueError) as exc:
            self._record_error(f"edit_markup_failed:{type(exc).__name__}:{exc}")

    # ------------------------------------------------------------------
    # Internals — auth + rate limit
    # ------------------------------------------------------------------
    def _is_allowed(self, callback_query: dict[str, Any]) -> bool:
        if self._allowed_chat_ids is None:
            return True
        from_obj = callback_query.get("from") or {}
        msg = callback_query.get("message") or {}
        chat = msg.get("chat") or {}
        candidates = {
            str(from_obj.get("id")) if from_obj.get("id") is not None else None,
            str(chat.get("id")) if chat.get("id") is not None else None,
        }
        candidates.discard(None)
        return any(c in self._allowed_chat_ids for c in candidates)

    def _strip_secret_prefix(self, callback_data: str) -> str | None:
        """Return ``callback_data`` minus the secret prefix, or ``None`` if invalid.

        When ``callback_secret`` is unset every payload passes through
        unchanged. When set, the prefix must match exactly *and* be
        followed by ``:`` — anything else is treated as a forgery
        attempt and ignored upstream.
        """
        if not self._callback_secret:
            return callback_data
        prefix = f"{self._callback_secret}:"
        if not callback_data.startswith(prefix):
            return None
        return callback_data[len(prefix):]

    def _parse_start_action(self, text: str) -> str | None:
        """Translate Telegram deep-link ``/start`` payloads to callback_data."""
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        command = parts[0].split("@", 1)[0].lower()
        if command != "/start" or len(parts) != 2:
            return None
        payload = urlparse.unquote(parts[1].strip())
        if not payload.startswith("case_"):
            return None

        body = payload[len("case_"):]
        suffix_map = {
            "_dry_publish": "dry-publish",
            "_write_stub": "write-stub",
            "_drop": "drop",
        }
        for suffix, action in suffix_map.items():
            if body.endswith(suffix):
                case_id = body[: -len(suffix)]
                return f"case:{action}:{case_id}" if case_id else None

        snooze_marker = "_snooze_"
        if snooze_marker in body:
            case_id, days = body.rsplit(snooze_marker, 1)
            if case_id and days:
                return f"case:snooze:{case_id}:{days}"
        return None

    def _rate_limit_ok(self, key: str) -> bool:
        """Sliding-window per-key check; mutates the bucket on accept."""
        now = time.monotonic()
        bucket = self._rate_buckets.setdefault(key, deque())
        cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max_actions_per_minute:
            return False
        bucket.append(now)
        return True

    # ------------------------------------------------------------------
    # Internals — main per-callback flow
    # ------------------------------------------------------------------
    def _handle_callback_query(self, cb: dict[str, Any]) -> bool:
        """Process one ``callback_query``. Returns ``True`` iff dispatched.

        The ``False`` return covers every short-circuit: missing id,
        missing data, blocked by ACL, blocked by secret, blocked by
        rate-limit, or dispatcher-reported failure. Successful
        dispatches always return ``True`` *and* bump
        ``actions_dispatched`` exactly once.
        """
        cb_id = cb.get("id")
        data = cb.get("data")
        if not cb_id or not isinstance(data, str) or not data:
            return False

        if len(data.encode("utf-8")) > _CALLBACK_DATA_HARD_LIMIT:
            # TG itself enforces 64 bytes on the sender side, but we
            # log defensively in case a future protocol update relaxes
            # it and we want to flag oversized payloads in ops review.
            _log.warning("callback_data exceeds 64-byte limit (len=%d)", len(data))

        # 1) Chat whitelist.
        if not self._is_allowed(cb):
            self._safe_answer(cb_id, text="Not authorized", show_alert=True)
            self._record_error(f"unauthorized:{(cb.get('from') or {}).get('id')}")
            return False

        # 2) Secret prefix.
        stripped = self._strip_secret_prefix(data)
        if stripped is None:
            _log.info("callback_data missing required secret prefix; ignoring")
            self._safe_answer(cb_id, text="Invalid callback")
            self._record_error("missing_secret_prefix")
            return False

        # 3) Rate-limit (per chat_id, falling back to user_id when chat absent).
        from_obj = cb.get("from") or {}
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id") if chat.get("id") is not None else from_obj.get("id")
        user_id = from_obj.get("id")
        message_id = msg.get("id") or msg.get("message_id")
        rl_key = str(chat_id if chat_id is not None else user_id or "anon")
        if not self._rate_limit_ok(rl_key):
            self._safe_answer(
                cb_id,
                text="Rate limited; try later",
                show_alert=True,
            )
            return False

        # 4) Dispatch.
        ctx = {
            "root": self._host_root,
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": message_id,
        }
        try:
            result = self._dispatcher(stripped, ctx)
        except Exception as exc:  # noqa: BLE001 - daemon must not die
            self._record_error(
                f"dispatch_exception:{type(exc).__name__}:{exc}"
            )
            self._safe_answer(cb_id, text="Internal error")
            return False

        if not isinstance(result, dict):
            self._record_error("dispatch_invalid_result_shape")
            self._safe_answer(cb_id, text="Internal error")
            return False

        success = bool(result.get("success"))
        summary = str(result.get("summary") or "")
        if success:
            self._safe_answer(cb_id, text=summary[:200] or "Done")
            self._safe_remove_buttons(chat_id, message_id)
            self._stats["actions_dispatched"] += 1
            self._stats["last_action_at"] = _utc_now_iso()
            return True

        # Failure path — surface the dispatcher's reason as a TG alert
        # so the operator gets immediate feedback instead of having to
        # ssh into the host to read logs.
        self._safe_answer(
            cb_id,
            text=f"❌ {summary[:180] or 'Action failed'}",
            show_alert=True,
        )
        return False

    def _handle_start_message(self, msg: dict[str, Any]) -> bool:
        """Process one Lark→Telegram deep-link ``/start`` message."""
        text = msg.get("text")
        if not isinstance(text, str) or not text:
            return False
        callback_data = self._parse_start_action(text)
        if not callback_data:
            return False

        wrapper = {"from": msg.get("from") or {}, "message": msg}
        chat = msg.get("chat") or {}
        from_obj = msg.get("from") or {}
        chat_id = chat.get("id") if chat.get("id") is not None else from_obj.get("id")
        user_id = from_obj.get("id")
        message_id = msg.get("id") or msg.get("message_id")

        if not self._is_allowed(wrapper):
            self._safe_send_message(
                chat_id,
                "Not authorized",
                reply_to_message_id=message_id,
            )
            self._record_error(f"unauthorized_start:{user_id}")
            return False

        rl_key = str(chat_id if chat_id is not None else user_id or "anon")
        if not self._rate_limit_ok(rl_key):
            self._safe_send_message(
                chat_id,
                "Rate limited; try later",
                reply_to_message_id=message_id,
            )
            return False

        ctx = {
            "root": self._host_root,
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": message_id,
        }
        try:
            result = self._dispatcher(callback_data, ctx)
        except Exception as exc:  # noqa: BLE001 - daemon must not die
            self._record_error(
                f"dispatch_exception:{type(exc).__name__}:{exc}"
            )
            self._safe_send_message(
                chat_id,
                "Internal error",
                reply_to_message_id=message_id,
            )
            return False

        if not isinstance(result, dict):
            self._record_error("dispatch_invalid_result_shape")
            self._safe_send_message(
                chat_id,
                "Internal error",
                reply_to_message_id=message_id,
            )
            return False

        success = bool(result.get("success"))
        summary = str(result.get("summary") or "")
        response = summary[:200] or "Done"
        if success:
            self._safe_send_message(chat_id, response, reply_to_message_id=message_id)
            self._stats["actions_dispatched"] += 1
            self._stats["last_action_at"] = _utc_now_iso()
            return True

        self._safe_send_message(
            chat_id,
            f"❌ {response[:180] or 'Action failed'}",
            reply_to_message_id=message_id,
        )
        return False

    # ------------------------------------------------------------------
    # Internals — stats / signals
    # ------------------------------------------------------------------
    def _record_error(self, msg: str) -> None:
        ts = _utc_now_iso()
        entry = f"[{ts}] {msg}"
        errs = self._stats["errors"]
        errs.append(entry)
        if len(errs) > _ERROR_HISTORY_CAP:
            del errs[0 : len(errs) - _ERROR_HISTORY_CAP]
        _log.warning("tg-listener: %s", msg)

    def _flush_stats_to_disk(self) -> None:
        path = _stats_path(self._host_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            _log.warning("failed to flush listener stats: %s", exc)

    def _install_signal_handlers(self) -> None:
        """Best-effort SIGTERM / SIGINT → ``stop()`` wiring.

        Only succeeds on the main thread; tests calling ``run_forever``
        from a worker thread silently skip and rely on ``stop()`` being
        invoked manually.
        """
        def _handler(signum, _frame):  # type: ignore[no-untyped-def]
            _log.info("tg-listener: caught signal %s; shutting down", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not on main thread, or signal unsupported on this platform.
                continue


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentflow-tg-listen",
        description=(
            "Long-poll Telegram bot for callback-button driven case actions."
        ),
    )
    p.add_argument(
        "--root",
        default=None,
        help="Host project root (defaults to cwd).",
    )
    p.add_argument(
        "--bot-token-env",
        default="TELEGRAM_BOT_TOKEN",
        help="Env var holding the TG bot token (default: TELEGRAM_BOT_TOKEN).",
    )
    p.add_argument(
        "--chat-id-allowlist",
        default=None,
        help="Comma-separated list of chat / user IDs allowed to trigger actions.",
    )
    p.add_argument(
        "--callback-secret-env",
        default="TELEGRAM_CALLBACK_SECRET",
        help="Env var holding the callback_data prefix secret.",
    )
    p.add_argument(
        "--long-poll-timeout",
        type=int,
        default=_DEFAULT_LONG_POLL_TIMEOUT,
        help="getUpdates long-poll timeout in seconds (default: 25).",
    )
    p.add_argument(
        "--max-actions-per-minute",
        type=int,
        default=_DEFAULT_MAX_ACTIONS_PER_MINUTE,
        help="Per-chat sliding-window rate limit (default: 10).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll iteration and exit (debug).",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Print current stats from disk and exit.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-warning log output.",
    )
    return p


def _parse_chat_allowlist(raw: str | None) -> list[int | str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    out: list[int | str] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(p)
    return out or None


def _print_stats(host_root: Path) -> int:
    path = _stats_path(host_root)
    if not path.exists():
        print(json.dumps({"error": "no stats file", "path": str(path)}))
        return 1
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(json.dumps({"error": str(exc), "path": str(path)}))
        return 1
    print(body)
    return 0


def _main_entry(argv: list[str] | None = None) -> int:
    """``agentflow-tg-listen`` console-script entry point."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    host_root = Path(args.root) if args.root else Path.cwd()

    if args.stats:
        return _print_stats(host_root)

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot_token = os.environ.get(args.bot_token_env, "").strip()
    if not bot_token:
        print(
            f"[tg-listener] ERROR {args.bot_token_env} env var is empty; refusing to start",
            file=sys.stderr,
        )
        return 2

    secret = os.environ.get(args.callback_secret_env, "").strip() or None
    allowlist = _parse_chat_allowlist(args.chat_id_allowlist)

    listener = TgCallbackListener(
        bot_token=bot_token,
        host_root=host_root,
        allowed_chat_ids=allowlist,
        callback_secret=secret,
        long_poll_timeout=args.long_poll_timeout,
        max_actions_per_minute=args.max_actions_per_minute,
    )

    if args.once:
        try:
            count = listener.run_once()
        except Exception as exc:  # noqa: BLE001 - CLI surface
            print(f"[tg-listener] run_once failed: {exc}", file=sys.stderr)
            print(json.dumps(listener.stats, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(listener.stats, ensure_ascii=False, indent=2))
        print(f"[tg-listener] dispatched={count}")
        return 0

    listener.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main_entry())
