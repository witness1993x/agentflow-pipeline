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
from .notification_templates import (
    DEFAULT_LARK_SCAN_CARD_TPL,
    render_scan_card,
    resolve_template,
)

_log = logging.getLogger("agentflow.lark_callback")

_ACTION_TO_CASE_VERB = {
    "git_case_dry_publish": "dry-publish",
    "git_case_write_stub": "write-stub",
    "git_case_fork_rewrite": "fork-rewrite",
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


def _case_id_from_promoted_case(case: dict[str, Any]) -> str:
    raw = str(case.get("hotspot_id") or case.get("case_id") or "")
    if raw.startswith("HSP-"):
        return raw
    case_dir = str(case.get("case_dir") or "")
    name = Path(case_dir).name
    if "-" in name and name.startswith("HSP-"):
        return "-".join(name.split("-")[:2])
    return raw


def _button(label: str, action: str, case_id: str, *, button_type: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "value": {
            "action": action,
            "case_id": case_id,
        },
    }


def build_scan_interactive_card(
    *,
    scan_result: dict[str, Any],
    shipped_repos: list[dict[str, Any]] | None = None,
    auto_promoted_cases: list[dict[str, Any]] | None = None,
    top_n: int = 5,
    brand_prefix: str = "",
    host_root: Path | str | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Render a Lark App interactive card with Git case action buttons.

    This is for OpenClaw/Lark App mode. Standalone Lark Custom Bot webhooks
    should keep using ``lark_notifier`` because they cannot receive callbacks.
    """
    host_path = Path(str(host_root)).expanduser() if host_root is not None else None
    template = resolve_template(name="lark_scan_card", host_root=host_path)
    title, body = render_scan_card(
        template=template or DEFAULT_LARK_SCAN_CARD_TPL,
        scan_result=scan_result,
        shipped_repos=shipped_repos or [],
        auto_promoted_cases=auto_promoted_cases or [],
        top_n=top_n,
        brand_prefix=brand_prefix,
    )
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"content": body, "tag": "lark_md"}},
    ]
    safe_root = str(Path(str(root)).expanduser()) if root is not None else ""
    for case in [c for c in (auto_promoted_cases or []) if isinstance(c, dict)]:
        case_id = _case_id_from_promoted_case(case)
        if not case_id:
            continue
        actions = [
            _button("✅ 8 gates", "git_case_dry_publish", case_id, button_type="primary"),
            _button("🔁 fork+rewrite", "git_case_fork_rewrite", case_id, button_type="primary"),
            _button("😴 snooze 7d", "git_case_snooze", case_id),
            _button("🗑 drop", "git_case_drop", case_id, button_type="danger"),
        ]
        if safe_root:
            for action in actions:
                action["value"]["root"] = safe_root
        elements.append({
            "tag": "action",
            "actions": actions,
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": title, "tag": "plain_text"},
            "template": "blue",
        },
        "elements": elements,
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


__all__ = ["build_scan_interactive_card", "handle_event"]
