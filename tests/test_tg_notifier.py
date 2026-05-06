"""Tests for ``agentflow_pipeline.tg_notifier``.

Every test mocks ``urllib.request.urlopen`` and / or monkeypatches the
``TELEGRAM_*`` env block. Zero real network access — running the suite
offline must still produce a green result.

Note on the L2 dependency: ``notify_scan_complete`` lazily imports
``agentflow_pipeline.notification_templates``. The L2 module API is
locked but the file itself may not exist yet on disk, so we install a
``sys.modules`` fake before the import happens.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any
from urllib import error as urlerror

import pytest

from agentflow_pipeline import tg_notifier
from agentflow_pipeline.tg_notifier import (
    TgSendResult,
    _build_inline_keyboard,
    _chunk_text,
    _escape_markdown_v2,
    _truncate_callback_data,
    notify_scan_complete,
    send_card,
    send_text,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self._body = json.dumps(
            body or {"ok": True, "result": {"message_id": 1}}
        ).encode("utf-8")

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

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        bodies: list[dict] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._body = body
        self._bodies = list(bodies) if bodies else None

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
        if self._bodies:
            body = self._bodies.pop(0)
        else:
            body = self._body
        return _FakeResponse(status=self._status, body=body)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every TELEGRAM_* env var so each test starts from a known state."""
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_DRY_RUN",
    ):
        monkeypatch.delenv(key, raising=False)
    # Reset the module-level rate-limit clock so consecutive tests don't
    # accidentally sleep.
    tg_notifier._last_send_at = 0.0
    return monkeypatch


@pytest.fixture
def configured_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    clean_env.setenv("TELEGRAM_BOT_TOKEN", "TEST_TOKEN_123")
    clean_env.setenv("TELEGRAM_CHAT_ID", "-100123456")
    return clean_env


# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------
class TestEscapeMarkdownV2:
    def test_escapes_every_documented_special(self) -> None:
        # Per https://core.telegram.org/bots/api#markdownv2-style
        specials = "_*[]()~`>#+-=|{}.!\\"
        escaped = _escape_markdown_v2(specials)
        # Each special char must be preceded by a backslash.
        for ch in specials:
            assert f"\\{ch}" in escaped, f"missing escape for {ch!r}"
        # Length must double (every char gets a backslash prefix).
        assert len(escaped) == len(specials) * 2

    def test_passthrough_for_ordinary_text(self) -> None:
        assert _escape_markdown_v2("hello world 你好") == "hello world 你好"

    def test_empty_string(self) -> None:
        assert _escape_markdown_v2("") == ""


class TestChunkText:
    def test_short_text_stays_one_chunk(self) -> None:
        chunks = _chunk_text("hi", max_size=4000)
        assert chunks == ["hi"]

    def test_exact_length_one_chunk(self) -> None:
        text = "a" * 4000
        chunks = _chunk_text(text, max_size=4000)
        assert chunks == [text]

    def test_5000_char_splits(self) -> None:
        # Pure 'a's leave no paragraph / sentence boundary, so we get a
        # hard cut at max_size.
        text = "a" * 5000
        chunks = _chunk_text(text, max_size=4000)
        assert len(chunks) == 2
        assert all(len(c) <= 4000 for c in chunks)
        assert "".join(chunks) == text

    def test_8000_char_paragraphs_split_at_boundary(self) -> None:
        para = ("x" * 1000 + "\n\n") * 8  # 8 KB total with paragraph breaks
        chunks = _chunk_text(para, max_size=4000)
        # Every chunk must be within the cap.
        assert all(len(c) <= 4000 for c in chunks)
        # We should get at least 2 chunks.
        assert len(chunks) >= 2

    def test_chinese_split(self) -> None:
        sentence = "你好世界。" * 1000  # 6 chars * 1000 = 6000 chars
        chunks = _chunk_text(sentence, max_size=4000)
        assert all(len(c) <= 4000 for c in chunks)
        assert len(chunks) >= 2


class TestCallbackTruncation:
    def test_under_limit_passes_through(self) -> None:
        assert _truncate_callback_data("case:approve:HSP-1") == "case:approve:HSP-1"

    def test_over_limit_truncated(self) -> None:
        long = "x" * 100
        out = _truncate_callback_data(long)
        assert len(out.encode("utf-8")) <= 64
        assert out == "x" * 64


# ---------------------------------------------------------------------------
# send_text
# ---------------------------------------------------------------------------
def test_send_text_no_token_returns_skipped(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TELEGRAM_CHAT_ID", "-100123")
    result = send_text("hello")
    assert result["sent"] is False
    assert result["skipped_reason"] == "no_token_configured"
    assert result["message_id"] is None
    assert result["body_size_chars"] == 5


def test_send_text_no_chat_id_returns_skipped(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("TELEGRAM_BOT_TOKEN", "abc")
    result = send_text("hello")
    assert result["sent"] is False
    assert result["skipped_reason"] == "no_chat_id"
    assert result["chat_id"] is None


def test_send_text_dry_run_does_not_call_urlopen(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_text("hello", dry_run=True)
    assert result["sent"] is False
    assert result["skipped_reason"] == "dry_run"
    assert spy.calls == []


def test_send_text_real_send_returns_message_id(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy(body={"ok": True, "result": {"message_id": 123}})
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_text("hello world")
    assert result["sent"] is True
    assert result["skipped_reason"] is None
    assert result["message_id"] == 123
    assert result["chat_id"] == -100123456
    # Verify URL contains the bot token path and method name.
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["url"].endswith("/bot TEST_TOKEN_123/sendMessage".replace(" ", ""))
    assert call["payload"]["text"] == "hello world"
    assert call["payload"]["chat_id"] == -100123456
    assert call["payload"]["parse_mode"] == "MarkdownV2"


def test_send_text_chat_id_kwarg_overrides_env(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_text("hi", chat_id=999)
    assert result["sent"] is True
    assert spy.calls[0]["payload"]["chat_id"] == 999


# ---------------------------------------------------------------------------
# send_card
# ---------------------------------------------------------------------------
def test_send_card_url_actions_built_into_inline_keyboard(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_card(
        title="Daily scan",
        body_md="body here",
        url_actions=[("repo", "https://example.com/r"), ("docs", "https://example.com/d")],
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    rm = payload["reply_markup"]
    assert "inline_keyboard" in rm
    flat = [b for row in rm["inline_keyboard"] for b in row]
    assert any(b.get("url") == "https://example.com/r" for b in flat)
    assert any(b.get("url") == "https://example.com/d" for b in flat)
    # Title must be markdown-bold escaped.
    assert payload["text"].startswith("*Daily scan*")


def test_send_card_callback_data_truncated_when_over_64_bytes(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    huge = "x" * 200  # 200 bytes
    result = send_card(
        title="t",
        body_md="b",
        callback_actions=[("ack", huge)],
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    cb_buttons = [
        b for row in payload["reply_markup"]["inline_keyboard"]
        for b in row
        if "callback_data" in b
    ]
    assert len(cb_buttons) == 1
    assert len(cb_buttons[0]["callback_data"].encode("utf-8")) <= 64


def test_send_card_mixed_url_and_callback_buttons(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_card(
        title="t",
        body_md="b",
        url_actions=[("link", "https://example.com")],
        callback_actions=[("approve", "case:approve:HSP-1")],
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    flat = [
        b for row in payload["reply_markup"]["inline_keyboard"] for b in row
    ]
    assert any("url" in b for b in flat)
    assert any("callback_data" in b for b in flat)


def test_send_card_long_body_chunks_and_only_first_carries_keyboard(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy(
        bodies=[
            {"ok": True, "result": {"message_id": 100}},
            {"ok": True, "result": {"message_id": 101}},
        ],
    )
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    long_body = "x" * 5000
    result = send_card(
        title="t",
        body_md=long_body,
        url_actions=[("r", "https://x.example")],
    )
    assert result["sent"] is True
    assert result["message_id"] == 100  # first chunk
    assert len(spy.calls) >= 2
    # First chunk gets reply_markup, subsequent ones do not.
    assert "reply_markup" in spy.calls[0]["payload"]
    for call in spy.calls[1:]:
        assert "reply_markup" not in call["payload"]


def test_send_card_no_buttons_omits_reply_markup(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    send_card(title="t", body_md="b")
    assert "reply_markup" not in spy.calls[0]["payload"]


# ---------------------------------------------------------------------------
# Inline keyboard helper
# ---------------------------------------------------------------------------
def test_build_inline_keyboard_rows_max_3_per_row() -> None:
    urls = [(f"l{i}", f"https://x.example/{i}") for i in range(7)]
    rm = _build_inline_keyboard(
        url_actions=urls, callback_actions=None, max_total=8,
    )
    assert rm is not None
    rows = rm["inline_keyboard"]
    # 7 buttons → 3 / 3 / 1
    assert [len(r) for r in rows] == [3, 3, 1]


def test_build_inline_keyboard_drops_empty_entries() -> None:
    rm = _build_inline_keyboard(
        url_actions=[("", "https://x"), ("ok", ""), ("good", "https://y")],
        callback_actions=None,
    )
    assert rm is not None
    flat = [b for r in rm["inline_keyboard"] for b in r]
    assert len(flat) == 1
    assert flat[0]["text"] == "good"


# ---------------------------------------------------------------------------
# notify_scan_complete (with mocked notification_templates)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_templates(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``notification_templates`` module in ``sys.modules``.

    Production ``render_scan_card`` will be provided by L2 — we stub it
    so tests don't depend on a parallel agent's commit landing first.
    The fake records its kwargs so we can assert on them.
    """
    fake = types.ModuleType("agentflow_pipeline.notification_templates")
    fake.calls = []  # type: ignore[attr-defined]

    def resolve_template(*, name: str) -> str:
        fake.calls.append(("resolve_template", {"name": name}))  # type: ignore[attr-defined]
        return f"<TEMPLATE:{name}>"

    def render_scan_card(
        *,
        template: str,
        scan_result: dict,
        shipped_repos: list[dict],
        auto_promoted_cases: list[dict] | None = None,
        top_n: int = 5,
        brand_prefix: str = "",
    ) -> tuple[str, str]:
        fake.calls.append((  # type: ignore[attr-defined]
            "render_scan_card",
            {
                "template": template,
                "scan_result": scan_result,
                "shipped_repos": shipped_repos,
                "top_n": top_n,
                "auto_promoted_cases": auto_promoted_cases,
                "brand_prefix": brand_prefix,
            },
        ))
        title = "AgentFlow scan (10:00)"
        body = "**summary**\\n\\nbody contents"
        return title, body

    fake.resolve_template = resolve_template  # type: ignore[attr-defined]
    fake.render_scan_card = render_scan_card  # type: ignore[attr-defined]

    # Override even if the real module has already been imported elsewhere
    # in this pytest session — the lazy import inside notify_scan_complete
    # otherwise pulls the cached real module. We must patch BOTH
    # ``sys.modules`` AND the parent package attribute, since
    # ``from agentflow_pipeline import notification_templates`` resolves
    # via the attribute lookup once the parent package has cached it.
    import agentflow_pipeline as _pkg
    monkeypatch.setitem(
        sys.modules, "agentflow_pipeline.notification_templates", fake,
    )
    monkeypatch.setattr(_pkg, "notification_templates", fake, raising=False)
    return fake


def test_notify_scan_complete_full_path(
    configured_env: pytest.MonkeyPatch,
    fake_templates,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)

    scan_result = {
        "scanned_at": "2026-05-01T10:00:00+00:00",
        "by_source": {"github": 12, "hackernews": 3, "reddit": 2},
        "unique_count": 17,
        "duplicates_merged": 4,
        "top": [],
    }
    shipped = [
        {
            "name": "alpha-repo",
            "url": "https://github.com/x/alpha-repo",
            "language": "TypeScript",
            "shape": "data_pipeline",
            "hotspot_id": "HSP-001",
        },
        {
            "name": "beta-repo",
            "url": "https://github.com/x/beta-repo",
            "language": "Python",
            "shape": "service",
            "hotspot_id": "HSP-002",
        },
    ]
    promoted = [
        {
            "hotspot_id": "HSP-005",
            "hotspot_name": "promoted thing",
            "case_dir": "/x/cases/HSP-005",
            "source_url": "https://example.com/hsp-005",
        },
    ]
    result = notify_scan_complete(
        scan_result=scan_result,
        shipped_repos=shipped,
        framework_repo_url="https://github.com/witness1993x/agentflow-pipeline",
        trends_view_url="https://example.com/trends/scan.md",
        auto_promoted_cases=promoted,
    )
    assert result["sent"] is True

    payload = spy.calls[0]["payload"]
    text = payload["text"]
    # Title is escaped per MarkdownV2 (the dot in "(10:00)" should stay
    # but the colon doesn't need escaping; the parentheses do).
    assert text.startswith("*AgentFlow scan ")
    # Bold marker present.
    assert text.split("\n", 1)[0].startswith("*") and text.split("\n", 1)[0].endswith("*")
    # Parens in title escaped.
    assert "\\(10:00\\)" in text

    # Buttons: URL links plus Git-case callback actions. The callback
    # namespace is `case:*`, distinct from the article package's Gate A/B/C/D.
    rm = payload["reply_markup"]
    flat = [b for r in rm["inline_keyboard"] for b in r]
    labels = [b["text"] for b in flat]
    assert any("framework" in l for l in labels)
    assert any("alpha-repo" in l for l in labels)
    assert any("beta-repo" in l for l in labels)
    assert any("Git case" in l for l in labels)
    assert any("scan.md" in l for l in labels)
    assert any(b.get("callback_data") == "case:dry-publish:HSP-005" for b in flat)
    assert any(b.get("callback_data") == "case:fork-rewrite:HSP-005" for b in flat)
    assert any(b.get("callback_data") == "case:write-stub:HSP-005" for b in flat)
    assert any(b.get("callback_data") == "case:snooze:HSP-005:7d" for b in flat)
    assert all(
        not str(b.get("callback_data", "")).startswith(("A:", "B:", "C:", "D:"))
        for b in flat
    )
    # Total <= Telegram notifier cap.
    assert len(flat) <= tg_notifier._TG_MAX_BUTTONS_TOTAL

    # And the templates module was actually consulted with name=tg_scan_card.
    names = [c[1].get("name") for c in fake_templates.calls if c[0] == "resolve_template"]
    assert "tg_scan_card" in names


def test_notify_scan_complete_no_promoted_no_promoted_button(
    configured_env: pytest.MonkeyPatch,
    fake_templates,
) -> None:
    spy = _UrlopenSpy()
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)

    scan_result = {
        "scanned_at": "2026-05-01T10:00:00+00:00",
        "by_source": {"github": 1},
        "unique_count": 1,
        "duplicates_merged": 0,
        "top": [],
    }
    result = notify_scan_complete(
        scan_result=scan_result,
        shipped_repos=[],
        auto_promoted_cases=None,
    )
    assert result["sent"] is True
    payload = spy.calls[0]["payload"]
    rm = payload.get("reply_markup", {})
    flat = [b for r in rm.get("inline_keyboard", []) for b in r]
    labels = [b["text"] for b in flat]
    assert not any("Git case" in l for l in labels)
    assert not any("callback_data" in b for b in flat)


# ---------------------------------------------------------------------------
# Failure-mode handling
# ---------------------------------------------------------------------------
def test_send_text_url_error_does_not_propagate(
    configured_env: pytest.MonkeyPatch,
) -> None:
    def _explode(*_a, **_k):
        raise urlerror.URLError("boom")

    configured_env.setattr(tg_notifier.urlrequest, "urlopen", _explode)
    result = send_text("hello")
    assert result["sent"] is False
    assert result["skipped_reason"] is not None
    assert result["skipped_reason"].startswith("post_failed:")


def test_send_text_api_returns_ok_false_yields_post_failed_with_description(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy(
        body={"ok": False, "description": "Bad Request: chat not found", "error_code": 400},
    )
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_text("hi")
    assert result["sent"] is False
    reason = result["skipped_reason"] or ""
    assert reason.startswith("post_failed:")
    assert "chat not found" in reason


def test_send_text_http_500_yields_post_failed(
    configured_env: pytest.MonkeyPatch,
) -> None:
    spy = _UrlopenSpy(status=500, body={"ok": True})
    configured_env.setattr(tg_notifier.urlrequest, "urlopen", spy)
    result = send_text("hi")
    assert result["sent"] is False
    assert (result["skipped_reason"] or "").startswith("post_failed:http_500")
