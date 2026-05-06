"""Tests for ``agentflow_pipeline.lark_notifier``.

Every test mocks ``urllib.request.urlopen`` and / or monkeypatches the
``LARK_WEBHOOK_*`` env block. Zero real network access — running the
suite offline must still produce a green result.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib import error as urlerror

import pytest

from agentflow_pipeline import lark_notifier
from agentflow_pipeline.lark_notifier import (
    LarkSendResult,
    _ensure_keyword,
    _in_rate_limit_zone,
    _sign,
    _truncate,
    notify_scan_complete,
    send_card,
    send_text,
)


# ---------------------------------------------------------------------------
# Common test helpers
# ---------------------------------------------------------------------------
class _FakeResponse:
    """Stand-in for the urlopen context-manager return value."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self._body = json.dumps(body or {"code": 0}).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _UrlopenSpy:
    """Records every (url, payload) that gets POSTed."""

    def __init__(self, *, status: int = 200, body: dict | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._body = body

    def __call__(self, request, timeout: int = 10):  # noqa: ARG002
        url = request.full_url
        data = request.data or b""
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self.calls.append({
            "url": url,
            "payload": payload,
            "headers": dict(request.headers),
            "method": request.get_method(),
        })
        return _FakeResponse(status=self._status, body=self._body)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every LARK_* env var so each test starts from a known state."""
    for key in (
        "LARK_WEBHOOK_URL",
        "LARK_WEBHOOK_SECRET",
        "LARK_WEBHOOK_KEYWORDS",
        "LARK_WEBHOOK_BRAND_PREFIX",
        "LARK_WEBHOOK_NO_DEFER",
        "LARK_WEBHOOK_DRY_RUN",
    ):
        monkeypatch.delenv(key, raising=False)
    # Disable the HH:00 / HH:30 deferral globally for tests so they don't
    # accidentally sleep when the test clock happens to land in the zone.
    monkeypatch.setenv("LARK_WEBHOOK_NO_DEFER", "true")
    return monkeypatch


@pytest.fixture
def scan_fixture() -> dict:
    return {
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
                "title": "Show HN: tiny vector DB",
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


@pytest.fixture
def shipped_fixture() -> list[dict]:
    return [
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
        {
            "name": "stable-depeg-radar",
            "url": "https://github.com/example/stable-depeg-radar",
            "language": "Python",
            "shape": "alert_bot",
            "hotspot_id": "HSP-003",
        },
    ]


# ---------------------------------------------------------------------------
# 1. _sign — byte-for-byte fixture
# ---------------------------------------------------------------------------
def test_sign_byte_for_byte_matches_reference() -> None:
    # Reference computed via the production lark_webhook impl with
    #   ts=1714521600 secret='super-secret-key'
    #   HmacSHA256(key=f"{ts}\n{secret}", msg=b"") then b64.
    expected = "Or+xH2UFntBl2oABBJPvWmFWoPdgjfwMV3RpzyT5F2Q="
    assert _sign(1714521600, "super-secret-key") == expected


# ---------------------------------------------------------------------------
# 2. _ensure_keyword — three branches
# ---------------------------------------------------------------------------
def test_ensure_keyword_already_present() -> None:
    text = "the [agentflow] daily scan"
    assert _ensure_keyword(text, ["agentflow", "scan"]) == text


def test_ensure_keyword_missing_appends_first() -> None:
    out = _ensure_keyword("hello world", ["agentflow", "scan"])
    assert out.endswith("[agentflow]")
    assert "hello world" in out


def test_ensure_keyword_empty_keyword_list_noop() -> None:
    assert _ensure_keyword("hello", []) == "hello"
    assert _ensure_keyword("hello", ["", "  "]) == "hello"


# ---------------------------------------------------------------------------
# 3. _in_rate_limit_zone — five clock points
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "minute,second,expected",
    [
        (0, 0, True),    # exactly HH:00:00
        (0, 30, True),   # HH:00:30 still in zone
        (1, 1, False),   # HH:01:01 -> 61s past, out
        (30, 30, True),  # HH:30:30 in zone
        (15, 0, False),  # HH:15:00 well outside both zones
    ],
)
def test_in_rate_limit_zone_clock_points(minute: int, second: int, expected: bool) -> None:
    now = datetime(2026, 5, 1, 10, minute, second)
    assert _in_rate_limit_zone(now) is expected


# ---------------------------------------------------------------------------
# 4. _truncate — text body and card body
# ---------------------------------------------------------------------------
def test_truncate_text_body_caps_long_string() -> None:
    big = "x" * 30_000
    payload = {"msg_type": "text", "content": {"text": big}}
    out = _truncate(payload)
    serialized = json.dumps(out, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= 19_000
    assert "truncated for Lark 20K cap" in out["content"]["text"]


def test_truncate_card_body_caps_longest_element() -> None:
    big = "y" * 30_000
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": "t", "tag": "plain_text"}, "template": "blue"},
            "elements": [
                {"tag": "div", "text": {"content": "short", "tag": "lark_md"}},
                {"tag": "div", "text": {"content": big, "tag": "lark_md"}},
            ],
        },
    }
    out = _truncate(payload)
    serialized = json.dumps(out, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= 19_000
    # Only the long element got trimmed.
    assert out["card"]["elements"][0]["text"]["content"] == "short"
    assert "truncated for Lark 20K cap" in out["card"]["elements"][1]["text"]["content"]


# ---------------------------------------------------------------------------
# 5–7. send_text behaviour
# ---------------------------------------------------------------------------
def test_send_text_no_url_skips_silently(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)
    result = send_text("hello")
    assert result["sent"] is False
    assert result["skipped_reason"] == "no_url_configured"
    assert spy.calls == []


def test_send_text_dry_run_skips_network(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)
    result = send_text("hello world", dry_run=True)
    assert result["sent"] is False
    assert result["skipped_reason"] == "dry_run"
    assert spy.calls == []
    assert result["body_size_bytes"] > 0


def test_send_text_real_post_emits_text_msg(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy(status=200, body={"code": 0})
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)
    result = send_text("hello there")
    assert result["sent"] is True
    assert result["skipped_reason"] is None
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["url"] == "https://example.com/lark/hook"
    assert call["method"] == "POST"
    assert call["payload"]["msg_type"] == "text"
    assert call["payload"]["content"]["text"] == "hello there"
    # Default content-type header.
    assert any(
        k.lower() == "content-type" and "json" in v.lower()
        for k, v in call["headers"].items()
    )


# ---------------------------------------------------------------------------
# 8. send_card with brand prefix
# ---------------------------------------------------------------------------
def test_send_card_includes_brand_prefix(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    monkeypatch.setenv("LARK_WEBHOOK_BRAND_PREFIX", "AgentFlow")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    result = send_card(
        title="hi",
        body_md="**body**",
        url_actions=[("ok", "https://x.test")],
        accent="green",
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    assert payload["msg_type"] == "interactive"
    title = payload["card"]["header"]["title"]["content"]
    assert title.startswith("[AgentFlow] ")
    assert title.endswith("hi")
    assert payload["card"]["header"]["template"] == "green"


# ---------------------------------------------------------------------------
# 9. send_card filters empty url_actions
# ---------------------------------------------------------------------------
def test_send_card_filters_empty_actions(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)
    send_card(
        title="t",
        body_md="b",
        url_actions=[("", ""), ("ok", ""), ("", "https://x.test"), ("real", "https://y.test")],
    )
    payload = spy.calls[0]["payload"]
    elements = payload["card"]["elements"]
    # First element is the markdown div, second is the action block.
    action_blocks = [el for el in elements if el.get("tag") == "action"]
    assert len(action_blocks) == 1
    actions = action_blocks[0]["actions"]
    assert len(actions) == 1
    assert actions[0]["text"]["content"] == "real"
    assert actions[0]["url"] == "https://y.test"


# ---------------------------------------------------------------------------
# 10. notify_scan_complete happy path
# ---------------------------------------------------------------------------
def test_notify_scan_complete_happy_path(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    result = notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    body = payload["card"]["elements"][0]["text"]["content"]

    # Top section.
    assert "扫到 24 unique" in body
    assert "sstklen/trump-code" in body
    assert "Show HN: tiny vector DB" in body
    # Ship'd section.
    assert "chainstream-launch-radar (TypeScript, data_pipeline)" in body
    assert "whale-pulse-evm" in body
    assert "stable-depeg-radar (Python, alert_bot)" in body
    # scanned_at echoed somewhere.
    assert "2026-05-04" in body

    # Buttons: framework + 3 ship'd repos = 4 (no trends_view_url passed).
    action_block = next(el for el in payload["card"]["elements"] if el.get("tag") == "action")
    labels = [a["text"]["content"] for a in action_block["actions"]]
    assert labels[0] == "📚 framework repo"
    assert any("chainstream-launch-radar" in l for l in labels)
    assert len(labels) == 4

    # Title and accent.
    title = payload["card"]["header"]["title"]["content"]
    assert "每日热点扫描" in title
    assert payload["card"]["header"]["template"] == "blue"  # 24 unique → blue


# ---------------------------------------------------------------------------
# 11. notify_scan_complete with empty shipped list
# ---------------------------------------------------------------------------
def test_notify_scan_complete_empty_shipped(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)
    result = notify_scan_complete(scan_result=scan_fixture, shipped_repos=[])
    assert result["sent"] is True
    body = spy.calls[0]["payload"]["card"]["elements"][0]["text"]["content"]
    assert "尚未 ship 任何 repo" in body


# ---------------------------------------------------------------------------
# 12. notify_scan_complete with zero hotspots → grey + "暂无可写热点"
# ---------------------------------------------------------------------------
def test_notify_scan_complete_zero_unique(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    result = notify_scan_complete(
        scan_result={
            "scanned_at": "2026-05-04T10:00:00+00:00",
            "by_source": {"github": 0, "hackernews": 0, "reddit": 0},
            "unique_count": 0,
            "duplicates_merged": 0,
            "top": [],
        },
        shipped_repos=[],
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    assert payload["card"]["header"]["template"] == "grey"
    body = payload["card"]["elements"][0]["text"]["content"]
    assert "暂无可写热点" in body


# ---------------------------------------------------------------------------
# 13. button cap at 5
# ---------------------------------------------------------------------------
def test_notify_scan_complete_button_cap(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    six_shipped = [
        {
            "name": f"repo-{i}",
            "url": f"https://github.com/example/repo-{i}",
            "language": "Python",
            "shape": "alert_bot",
            "hotspot_id": f"HSP-{i:03d}",
        }
        for i in range(6)
    ]
    result = notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=six_shipped,
        trends_view_url="https://trends.example.com/scan.md",
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    action_block = next(el for el in payload["card"]["elements"] if el.get("tag") == "action")
    # Hard cap = 5; framework + first 3 ship'd + trends-view = 5.
    assert len(action_block["actions"]) == _max_buttons()


def _max_buttons() -> int:
    return lark_notifier._MAX_BUTTONS_PER_CARD  # exported via module attr


# ---------------------------------------------------------------------------
# 14. URLError → result.sent=False, no exception propagation
# ---------------------------------------------------------------------------
def test_send_text_url_error_swallowed(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")

    def boom(request, timeout: int = 10):  # noqa: ARG001
        raise urlerror.URLError("connection refused")

    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", boom)
    result = send_text("hello")
    assert result["sent"] is False
    assert result["skipped_reason"] is not None
    assert result["skipped_reason"].startswith("post_failed:")


# ---------------------------------------------------------------------------
# 15. LARK_WEBHOOK_DRY_RUN=true env equals dry_run kwarg
# ---------------------------------------------------------------------------
def test_env_dry_run_disables_network(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    monkeypatch.setenv("LARK_WEBHOOK_DRY_RUN", "true")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    result = send_text("hello")
    assert result["sent"] is False
    assert result["skipped_reason"] == "dry_run"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# 16. LarkSendResult exported from public API surface
# ---------------------------------------------------------------------------
def test_lark_send_result_typed_dict_keys() -> None:
    # TypedDict is an alias to dict at runtime — the contract is the
    # named keys appearing in __annotations__.
    annotations = LarkSendResult.__annotations__
    assert set(annotations.keys()) == {
        "sent",
        "skipped_reason",
        "card_title",
        "body_size_bytes",
    }


# ---------------------------------------------------------------------------
# 17. auto_promoted_cases — backwards compatibility (omitted == empty)
# ---------------------------------------------------------------------------
def test_notify_scan_complete_omitting_auto_promoted_matches_legacy(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    """No kwarg / None / [] all produce a byte-identical legacy card."""
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    notify_scan_complete(
        scan_result=scan_fixture, shipped_repos=shipped_fixture, top_n=3
    )
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=None,
    )
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=[],
    )
    assert len(spy.calls) == 3
    payloads = [c["payload"] for c in spy.calls]
    bodies = [p["card"]["elements"][0]["text"]["content"] for p in payloads]
    # All three renders must be byte-identical to the legacy baseline.
    assert bodies[0] == bodies[1] == bodies[2]
    # And critically must NOT mention the auto-promote section.
    assert "自动 promote" not in bodies[0]
    # Buttons identical too — same labels in the same order.
    label_sets = []
    for payload in payloads:
        action = next(
            el for el in payload["card"]["elements"] if el.get("tag") == "action"
        )
        label_sets.append([a["text"]["content"] for a in action["actions"]])
    assert label_sets[0] == label_sets[1] == label_sets[2]


def test_notify_scan_complete_omitting_tg_username_matches_legacy(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    """The TG bridge kwarg must be inert unless it has a non-empty username."""
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    promoted = [
        {
            "hotspot_id": "HSP-005",
            "hotspot_name": "evm-arb-bot",
            "case_dir": "/Users/op/agentflow/cases/HSP-005-evm-arb-bot",
            "source_url": "https://github.com/example/evm-arb-bot",
        }
    ]
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=promoted,
    )
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=promoted,
        tg_bot_deep_link_username="",
    )

    assert spy.calls[0]["payload"] == spy.calls[1]["payload"]


# ---------------------------------------------------------------------------
# 18. auto_promoted_cases — single case renders section + button + green accent
# ---------------------------------------------------------------------------
def test_notify_scan_complete_one_promoted_case(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    promoted = [
        {
            "hotspot_id": "HSP-005",
            "hotspot_name": "evm-arb-bot",
            "case_dir": "/Users/op/agentflow/cases/HSP-005-2026-05-04-evm-arb-bot",
            "source_url": "https://github.com/example/evm-arb-bot",
        }
    ]
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=promoted,
    )
    payload = spy.calls[0]["payload"]
    body = payload["card"]["elements"][0]["text"]["content"]

    # Section present with the right header + entry.
    assert "**📝 自动 promote 了 1 个新 case** (尚未写代码)" in body
    assert "`HSP-005`" in body
    assert "evm-arb-bot" in body
    # Relative path shown — drops the absolute prefix.
    assert "cases/HSP-005-2026-05-04-evm-arb-bot" in body
    assert "/Users/op/agentflow/" not in body
    # No "(+ N more)" line for a single case.
    assert "more)" not in body

    # Button present, labelled with count = 1, linking to source_url.
    action = next(
        el for el in payload["card"]["elements"] if el.get("tag") == "action"
    )
    labels = [a["text"]["content"] for a in action["actions"]]
    assert "📝 promoted [1]" in labels
    promoted_btn = next(
        a for a in action["actions"] if a["text"]["content"] == "📝 promoted [1]"
    )
    assert promoted_btn["url"] == "https://github.com/example/evm-arb-bot"

    # Accent bumped to green (unique_count > 0 + promoted non-empty).
    assert payload["card"]["header"]["template"] == "green"


def test_notify_scan_complete_promoted_case_can_link_to_tg_bot(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    promoted = [
        {
            "hotspot_id": "HSP-005",
            "hotspot_name": "evm-arb-bot",
            "case_dir": "/Users/op/agentflow/cases/HSP-005-evm-arb-bot",
            "source_url": "https://github.com/example/evm-arb-bot",
        }
    ]
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=promoted,
        tg_bot_deep_link_username="@agentflow_bot",
    )
    payload = spy.calls[0]["payload"]
    action = next(
        el for el in payload["card"]["elements"] if el.get("tag") == "action"
    )
    promoted_btn = next(
        a for a in action["actions"] if a["text"]["content"] == "📝 promoted [1]"
    )
    assert promoted_btn["url"] == (
        "https://t.me/agentflow_bot?start=case_HSP-005_dry_publish"
    )


# ---------------------------------------------------------------------------
# 19. auto_promoted_cases — 5 cases truncates to 3 + "(+ 2 more)"
# ---------------------------------------------------------------------------
def test_notify_scan_complete_five_promoted_cases_truncates(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    promoted = [
        {
            "hotspot_id": f"HSP-{100 + i:03d}",
            "hotspot_name": f"hotspot-name-{i}",
            "case_dir": f"/root/agentflow/cases/HSP-{100 + i:03d}-name-{i}",
            "source_url": f"https://example.com/src/{i}",
        }
        for i in range(5)
    ]
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=promoted,
    )
    payload = spy.calls[0]["payload"]
    body = payload["card"]["elements"][0]["text"]["content"]

    # Header reports the full count, not the truncated 3.
    assert "**📝 自动 promote 了 5 个新 case** (尚未写代码)" in body
    # First three cases listed.
    for i in range(3):
        assert f"HSP-{100 + i:03d}" in body
    # Fourth and fifth NOT listed in their own bullet.
    for i in range(3, 5):
        assert f"`HSP-{100 + i:03d}`" not in body
    # Overflow line.
    assert "(+ 2 more)" in body

    # Button label reflects the full count.
    action = next(
        el for el in payload["card"]["elements"] if el.get("tag") == "action"
    )
    labels = [a["text"]["content"] for a in action["actions"]]
    assert "📝 promoted [5]" in labels
    promoted_btn = next(
        a for a in action["actions"] if a["text"]["content"] == "📝 promoted [5]"
    )
    # Links to the FIRST case's source_url.
    assert promoted_btn["url"] == "https://example.com/src/0"


# ---------------------------------------------------------------------------
# 20. auto_promoted_cases — empty source_url → no button, but section renders
# ---------------------------------------------------------------------------
def test_notify_scan_complete_promoted_without_source_url_skips_button(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
    shipped_fixture: list[dict],
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    promoted = [
        {
            "hotspot_id": "HSP-009",
            "hotspot_name": "no-url-case",
            "case_dir": "/x/cases/HSP-009-no-url",
            "source_url": "",  # empty — no button should be added
        }
    ]
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=shipped_fixture,
        top_n=3,
        auto_promoted_cases=promoted,
    )
    payload = spy.calls[0]["payload"]
    body = payload["card"]["elements"][0]["text"]["content"]

    # Body section still rendered.
    assert "**📝 自动 promote 了 1 个新 case** (尚未写代码)" in body
    assert "`HSP-009`" in body
    assert "no-url-case" in body
    assert "cases/HSP-009-no-url" in body

    # But no promote button.
    action = next(
        el for el in payload["card"]["elements"] if el.get("tag") == "action"
    )
    labels = [a["text"]["content"] for a in action["actions"]]
    assert not any(l.startswith("📝 promoted") for l in labels)


# ---------------------------------------------------------------------------
# 21. button cap — 1 promoted + 4 shipped + trends_view → 5 with promote
#     winning the trends slot
# ---------------------------------------------------------------------------
def test_notify_scan_complete_promote_button_priority_in_cap(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    scan_fixture: dict,
) -> None:
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/lark/hook")
    spy = _UrlopenSpy()
    monkeypatch.setattr(lark_notifier.urlrequest, "urlopen", spy)

    four_shipped = [
        {
            "name": f"repo-{i}",
            "url": f"https://github.com/example/repo-{i}",
            "language": "Python",
            "shape": "alert_bot",
            "hotspot_id": f"HSP-{i:03d}",
        }
        for i in range(4)
    ]
    promoted = [
        {
            "hotspot_id": "HSP-099",
            "hotspot_name": "promoted-stub",
            "case_dir": "/r/cases/HSP-099-promoted-stub",
            "source_url": "https://example.com/promoted-src",
        }
    ]
    notify_scan_complete(
        scan_result=scan_fixture,
        shipped_repos=four_shipped,
        trends_view_url="https://trends.example.com/scan.md",
        auto_promoted_cases=promoted,
    )
    payload = spy.calls[0]["payload"]
    action = next(
        el for el in payload["card"]["elements"] if el.get("tag") == "action"
    )
    labels = [a["text"]["content"] for a in action["actions"]]

    # Hard-capped at 5.
    assert len(labels) == lark_notifier._MAX_BUTTONS_PER_CARD
    # Order: framework -> 3 shipped (HSP-000..002) -> promoted -> [trends DROPPED].
    assert labels[0] == "📚 framework repo"
    assert all(labels[i].startswith("⭐ repo-") for i in (1, 2, 3))
    assert labels[4] == "📝 promoted [1]"
    # Trends-view button got pushed past the cap.
    assert "📊 查看 scan.md" not in labels
