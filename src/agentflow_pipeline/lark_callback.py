"""OpenClaw/Lark App callback adapter for Git hotspot repo cases.

This module is intentionally *not* a Feishu channel implementation. The
official ``@larksuite/openclaw-lark`` plugin owns the Lark App gateway and
forwards card actions here as plain tool calls. Action names use a
``git_case_*`` vocabulary so they cannot collide with the article package's
``lark_gate_*`` / Gate A-B-C-D commands.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .case_actions import dispatch_callback_action

_log = logging.getLogger("agentflow.lark_callback")

_ACTION_TO_CASE_VERB = {
    "git_case_dry_publish": "dry-publish",
    "git_case_write_stub": "write-stub",
    "git_case_drop": "drop",
    "git_case_snooze": "snooze",
}


def _empty_response() -> dict[str, Any]:
    return {
        "ack": True,
        "reply_card": None,
        "reply_text": None,
        "side_effects": [],
    }


def _make_card(*, title: str, body: str, template: str = "blue") -> dict[str, Any]:
    """Build a minimal Lark interactive-card response payload."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": title, "tag": "plain_text"},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"content": body, "tag": "lark_md"}},
        ],
    }


def _actor_for(operator: dict[str, Any] | None) -> str:
    op = operator or {}
    open_id = str(op.get("open_id") or op.get("user_id") or "unknown")
    return f"lark:{open_id}"


def _root_from(root: Path | str | None, payload: dict[str, Any]) -> Path:
    raw = root or payload.get("root") or payload.get("host_root") or "."
    return Path(str(raw)).expanduser().resolve()


def _case_callback_data(
    *,
    action: str,
    case_id: str,
    payload: dict[str, Any],
) -> str | None:
    verb = _ACTION_TO_CASE_VERB.get(action)
    if not verb:
        return None
    if verb == "snooze":
        days = str(payload.get("days") or payload.get("duration") or "7d")
        return f"case:snooze:{case_id}:{days}"
    return f"case:{verb}:{case_id}"


def handle_event(
    *,
    event_kind: str = "card_action",
    case_id: str | None = None,
    action: str | None = None,
    payload: dict[str, Any] | None = None,
    operator: dict[str, Any] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Handle one OpenClaw-forwarded Lark action for a Git repo case.

    Supported actions:
    ``git_case_dry_publish``, ``git_case_write_stub``, ``git_case_snooze``,
    and ``git_case_drop``. The returned shape mirrors the article package's
    Lark callback adapter: ``ack`` plus optional ``reply_card`` / ``reply_text``.
    """
    response = _empty_response()
    safe_payload = payload or {}
    safe_action = str(action or "")
    safe_case_id = str(case_id or safe_payload.get("case_id") or "").strip()

    if event_kind not in {"card_action", "message"}:
        response["reply_text"] = f"Unsupported Lark event kind: {event_kind}"
        return response

    callback_data = _case_callback_data(
        action=safe_action,
        case_id=safe_case_id,
        payload=safe_payload,
    )
    if not callback_data:
        response["reply_text"] = f"Unknown Git case action: {safe_action}"
        response["side_effects"].append("unknown_action")
        return response

    result = dispatch_callback_action(
        callback_data,
        root=_root_from(root, safe_payload),
        actor=_actor_for(operator),
    )
    success = bool(result.get("success"))
    summary = str(result.get("summary") or "")
    resolved_case = str(result.get("case_id") or safe_case_id)
    response["side_effects"].append(str(result.get("action") or callback_data))
    response["reply_card"] = _make_card(
        title=(
            "Git case 操作完成"
            if success
            else "Git case 操作失败"
        ),
        body=(
            f"**Case**: `{resolved_case}`\n"
            f"**Action**: `{safe_action}`\n"
            f"**Result**: {summary or ('ok' if success else 'failed')}"
        ),
        template="green" if success else "red",
    )
    if not success:
        _log.info("Lark Git case action failed: %s -> %s", safe_action, summary)
    return response


__all__ = ["handle_event"]
