"""Tests for ``agentflow_pipeline.tg_callback_listener``.

Every test patches ``urllib.request.urlopen`` *and* ``time.sleep`` so
no real network or wall-clock waits ever happen — the suite must run
fully offline in well under a second. The fake transport mirrors
Telegram's Bot API: ``getUpdates`` returns whatever the test queued
on its ``updates`` list, every other method (``answerCallbackQuery``,
``editMessageReplyMarkup``) records the payload and returns ``ok=True``.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror

import pytest

from agentflow_pipeline import tg_callback_listener
from agentflow_pipeline.tg_callback_listener import (
    ListenerStats,
    TgCallbackListener,
    _main_entry,
    _parse_chat_allowlist,
)


# ---------------------------------------------------------------------------
# Fake TG transport
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = json.dumps(body or {"ok": True, "result": []}).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeTgServer:
    """Records all method calls and serves canned ``getUpdates`` results.

    Each test pushes update batches via :meth:`enqueue_updates` and then
    reads back the recorded ``answerCallbackQuery`` / ``editMessageReplyMarkup``
    payloads from :attr:`calls`.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._update_batches: list[list[dict[str, Any]]] = []
        self._raise_on_get_updates: Exception | None = None
        self._raise_on_get_updates_count: int = 0

    def enqueue_updates(self, updates: list[dict[str, Any]]) -> None:
        self._update_batches.append(updates)

    def raise_on_get_updates(self, exc: Exception, times: int = 1) -> None:
        self._raise_on_get_updates = exc
        self._raise_on_get_updates_count = times

    def __call__(self, request, timeout: int | float = 10):  # noqa: ARG002
        url = request.full_url
        method = url.rsplit("/", 1)[-1]
        try:
            payload = json.loads((request.data or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self.calls.append({"method": method, "payload": payload})

        if method == "getUpdates":
            if self._raise_on_get_updates and self._raise_on_get_updates_count > 0:
                self._raise_on_get_updates_count -= 1
                exc = self._raise_on_get_updates
                if self._raise_on_get_updates_count == 0:
                    self._raise_on_get_updates = None
                raise exc
            batch = self._update_batches.pop(0) if self._update_batches else []
            return _FakeResponse({"ok": True, "result": batch})

        # answerCallbackQuery, editMessageReplyMarkup, etc.
        return _FakeResponse({"ok": True, "result": True})

    def find_calls(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> FakeTgServer:
    server = FakeTgServer()
    monkeypatch.setattr(
        tg_callback_listener.urlrequest,
        "urlopen",
        server,
    )
    # Kill all sleeps so backoff loops don't slow tests down.
    monkeypatch.setattr(tg_callback_listener.time, "sleep", lambda *_a, **_kw: None)
    return server


@pytest.fixture
def host_root(tmp_path: Path) -> Path:
    (tmp_path / "trends").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_listener(
    host_root: Path,
    *,
    allowed_chat_ids: list[int | str] | None = None,
    callback_secret: str | None = None,
    max_actions_per_minute: int = 10,
    dispatcher=None,
) -> TgCallbackListener:
    return TgCallbackListener(
        bot_token="TEST:abc",
        host_root=host_root,
        allowed_chat_ids=allowed_chat_ids,
        callback_secret=callback_secret,
        long_poll_timeout=1,
        max_actions_per_minute=max_actions_per_minute,
        action_dispatcher=dispatcher
        or (
            lambda cb, ctx: {
                "action": "noop",
                "case_id": "HSP-1",
                "success": True,
                "summary": "ok",
                "follow_up": [],
            }
        ),
    )


def _callback_update(
    update_id: int,
    *,
    data: str,
    chat_id: int = 555,
    user_id: int = 777,
    message_id: int = 42,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cbq-{update_id}",
            "from": {"id": user_id, "first_name": "Op"},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


def _message_update(
    update_id: int,
    *,
    text: str,
    chat_id: int = 555,
    user_id: int = 777,
    message_id: int = 43,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id, "first_name": "Op"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_run_once_no_updates_returns_zero(fake_server: FakeTgServer, host_root: Path) -> None:
    listener = _make_listener(host_root)
    fake_server.enqueue_updates([])
    assert listener.run_once() == 0
    assert listener.stats["polls"] == 1
    assert listener.stats["callback_queries_received"] == 0
    assert listener.stats["actions_dispatched"] == 0


def test_run_once_dispatches_callback_with_callback_data(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def dispatcher(cb_data: str, ctx: dict[str, Any]) -> dict[str, Any]:
        seen.append((cb_data, ctx))
        return {
            "action": "case:dry-publish",
            "case_id": "HSP-7",
            "success": True,
            "summary": "dispatched HSP-7",
            "follow_up": [],
        }

    listener = _make_listener(host_root, dispatcher=dispatcher)
    fake_server.enqueue_updates([
        _callback_update(100, data="case:dry-publish:HSP-7"),
    ])
    assert listener.run_once() == 1
    assert len(seen) == 1
    assert seen[0][0] == "case:dry-publish:HSP-7"
    assert seen[0][1]["chat_id"] == 555
    assert seen[0][1]["user_id"] == 777
    assert seen[0][1]["root"] == host_root
    answers = fake_server.find_calls("answerCallbackQuery")
    assert any("dispatched HSP-7" in (c["payload"] or {}).get("text", "") for c in answers)
    edits = fake_server.find_calls("editMessageReplyMarkup")
    assert len(edits) == 1
    assert listener.stats["actions_dispatched"] == 1


def test_run_once_dispatches_lark_deep_link_start_message(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def dispatcher(cb_data: str, ctx: dict[str, Any]) -> dict[str, Any]:
        seen.append((cb_data, ctx))
        return {
            "action": "case:dry-publish",
            "case_id": "HSP-7",
            "success": True,
            "summary": "dry publish checked HSP-7",
            "follow_up": [],
        }

    listener = _make_listener(host_root, dispatcher=dispatcher)
    fake_server.enqueue_updates([
        _message_update(101, text="/start case_HSP-7_dry_publish"),
    ])

    assert listener.run_once() == 1
    assert seen[0][0] == "case:dry-publish:HSP-7"
    assert seen[0][1]["chat_id"] == 555
    assert seen[0][1]["user_id"] == 777
    assert listener.stats["callback_queries_received"] == 0
    assert listener.stats["actions_dispatched"] == 1

    messages = fake_server.find_calls("sendMessage")
    assert len(messages) == 1
    assert "dry publish checked HSP-7" in messages[0]["payload"]["text"]
    assert messages[0]["payload"]["reply_to_message_id"] == 43


def test_run_once_start_message_respects_allowlist(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    seen: list[str] = []
    listener = _make_listener(
        host_root,
        allowed_chat_ids=[999],
        dispatcher=lambda cb, ctx: seen.append(cb)
        or {"action": "x", "case_id": "y", "success": True, "summary": "ok", "follow_up": []},
    )
    fake_server.enqueue_updates([
        _message_update(102, text="/start case_HSP-7_dry_publish", chat_id=555),
    ])

    assert listener.run_once() == 0
    assert seen == []
    messages = fake_server.find_calls("sendMessage")
    assert any("Not authorized" in (c["payload"] or {}).get("text", "") for c in messages)
    assert any("unauthorized_start" in e for e in listener.stats["errors"])


def test_run_once_chat_id_not_in_allowlist_blocks_dispatch(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    seen: list[Any] = []
    listener = _make_listener(
        host_root,
        allowed_chat_ids=[999],
        dispatcher=lambda cb, ctx: seen.append(cb)
        or {"action": "x", "case_id": "y", "success": True, "summary": "ok", "follow_up": []},
    )
    fake_server.enqueue_updates([_callback_update(1, data="case:drop:HSP-1", chat_id=555)])
    assert listener.run_once() == 0
    assert seen == []
    answers = fake_server.find_calls("answerCallbackQuery")
    assert any(
        (c["payload"] or {}).get("text", "").lower().startswith("not authorized")
        for c in answers
    )
    assert any((c["payload"] or {}).get("show_alert") is True for c in answers)


def test_run_once_allowlist_accepts_user_id_match(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    """Allowlist matches either chat.id or from.id — useful for direct DMs."""
    seen: list[str] = []
    listener = _make_listener(
        host_root,
        allowed_chat_ids=[777],  # user_id but not chat_id
        dispatcher=lambda cb, ctx: (seen.append(cb) or {
            "action": "x",
            "case_id": "y",
            "success": True,
            "summary": "ok",
            "follow_up": [],
        }),
    )
    fake_server.enqueue_updates([
        _callback_update(1, data="case:drop:HSP-1", chat_id=555, user_id=777),
    ])
    assert listener.run_once() == 1
    assert seen == ["case:drop:HSP-1"]


def test_run_once_secret_set_data_missing_prefix_is_ignored(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    seen: list[str] = []
    listener = _make_listener(
        host_root,
        callback_secret="s3cr3t",
        dispatcher=lambda cb, ctx: seen.append(cb)
        or {"action": "x", "case_id": "y", "success": True, "summary": "ok", "follow_up": []},
    )
    fake_server.enqueue_updates([
        _callback_update(1, data="case:drop:HSP-1"),  # no secret prefix
    ])
    assert listener.run_once() == 0
    assert seen == []
    assert any("missing_secret_prefix" in e for e in listener.stats["errors"])


def test_run_once_secret_set_data_with_prefix_is_stripped(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    seen: list[str] = []

    def dispatcher(cb_data: str, ctx: dict[str, Any]) -> dict[str, Any]:
        seen.append(cb_data)
        return {
            "action": "case:drop",
            "case_id": "HSP-1",
            "success": True,
            "summary": "dropped",
            "follow_up": [],
        }

    listener = _make_listener(
        host_root,
        callback_secret="s3cr3t",
        dispatcher=dispatcher,
    )
    fake_server.enqueue_updates([
        _callback_update(1, data="s3cr3t:case:drop:HSP-1"),
    ])
    assert listener.run_once() == 1
    # Dispatcher should see the *stripped* payload, not the prefixed one.
    assert seen == ["case:drop:HSP-1"]


def test_run_once_dispatcher_raises_does_not_crash(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    def dispatcher(cb: str, ctx: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("simulated handler boom")

    listener = _make_listener(host_root, dispatcher=dispatcher)
    fake_server.enqueue_updates([_callback_update(1, data="case:drop:HSP-1")])
    # Must NOT raise.
    count = listener.run_once()
    assert count == 0
    answers = fake_server.find_calls("answerCallbackQuery")
    assert any(
        (c["payload"] or {}).get("text", "").lower().startswith("internal error")
        for c in answers
    )
    assert any("dispatch_exception" in e for e in listener.stats["errors"])


def test_run_once_dispatcher_success_true_sends_summary(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    listener = _make_listener(
        host_root,
        dispatcher=lambda cb, ctx: {
            "action": "case:dry-publish",
            "case_id": "HSP-9",
            "success": True,
            "summary": "draft PR opened: example/repo#5",
            "follow_up": [],
        },
    )
    fake_server.enqueue_updates([_callback_update(1, data="case:dry-publish:HSP-9")])
    assert listener.run_once() == 1
    answers = fake_server.find_calls("answerCallbackQuery")
    assert len(answers) == 1
    assert "draft PR opened" in answers[0]["payload"]["text"]
    # Success path must NOT raise show_alert; default toast is enough.
    assert answers[0]["payload"].get("show_alert") is not True


def test_run_once_dispatcher_success_false_shows_alert_with_x(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    listener = _make_listener(
        host_root,
        dispatcher=lambda cb, ctx: {
            "action": "case:write-stub",
            "case_id": "HSP-3",
            "success": False,
            "summary": "stub already exists",
            "follow_up": [],
        },
    )
    fake_server.enqueue_updates([_callback_update(1, data="case:write-stub:HSP-3")])
    assert listener.run_once() == 0
    answers = fake_server.find_calls("answerCallbackQuery")
    assert len(answers) == 1
    payload = answers[0]["payload"]
    assert "stub already exists" in payload["text"]
    assert payload["text"].startswith("❌")
    assert payload.get("show_alert") is True
    # Failure path must NOT strip the inline keyboard — operator may want to retry.
    assert fake_server.find_calls("editMessageReplyMarkup") == []


def test_run_once_rate_limit_exceeded_sends_rate_limited_alert(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    listener = _make_listener(
        host_root,
        max_actions_per_minute=2,
        dispatcher=lambda cb, ctx: {
            "action": "x",
            "case_id": "y",
            "success": True,
            "summary": "ok",
            "follow_up": [],
        },
    )
    # 3 callbacks from the same chat — third must trip the limit.
    fake_server.enqueue_updates([
        _callback_update(1, data="case:drop:HSP-1"),
        _callback_update(2, data="case:drop:HSP-2"),
        _callback_update(3, data="case:drop:HSP-3"),
    ])
    listener.run_once()
    answers = fake_server.find_calls("answerCallbackQuery")
    rl_answers = [
        c for c in answers
        if "rate limited" in (c["payload"] or {}).get("text", "").lower()
    ]
    assert len(rl_answers) == 1
    assert rl_answers[0]["payload"].get("show_alert") is True
    assert listener.stats["actions_dispatched"] == 2


def test_run_forever_signal_stop_writes_stats_to_disk(
    fake_server: FakeTgServer, host_root: Path
) -> None:
    listener = _make_listener(host_root)
    # First poll empties the queue, second one we'll set the stop event
    # *before* it returns by hooking the urlopen call.
    poll_count = {"n": 0}
    real_call = fake_server.__call__

    def hooked(request, timeout: int | float = 10):
        poll_count["n"] += 1
        if poll_count["n"] >= 2:
            listener.stop()
        return real_call(request, timeout=timeout)

    # Replace the previously-installed urlopen (set by fake_server fixture).
    import agentflow_pipeline.tg_callback_listener as mod
    mod.urlrequest.urlopen = hooked  # type: ignore[assignment]

    # Run forever in this thread (no signal handlers will fire on test thread,
    # but we trigger stop() explicitly via the hook).
    listener.run_forever()

    stats_path = host_root / "trends" / "_listener.stats.json"
    assert stats_path.exists()
    written = json.loads(stats_path.read_text(encoding="utf-8"))
    assert written["polls"] >= 1
    assert "started_at" in written


def test_run_forever_urlerror_backs_off_and_continues(
    fake_server: FakeTgServer, host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = _make_listener(host_root)
    # First poll raises URLError (network blip); second poll is a normal
    # empty result and we tell the loop to stop afterwards.
    fake_server.raise_on_get_updates(urlerror.URLError("dns hiccup"), times=1)
    fake_server.enqueue_updates([])  # second poll returns clean empty

    poll_count = {"n": 0}

    # Override stop_event.wait to count cycles and stop after 2 polls.
    real_wait = listener._stop_event.wait

    def fake_wait(timeout: float | None = None) -> bool:
        poll_count["n"] += 1
        if poll_count["n"] >= 1:
            listener.stop()
        return real_wait(0)

    monkeypatch.setattr(listener._stop_event, "wait", fake_wait)
    listener.run_forever()
    # The URLError must have been recorded but the loop must NOT have crashed.
    assert any("get_updates_failed" in e for e in listener.stats["errors"])
    # And stats must have been flushed to disk.
    stats_path = host_root / "trends" / "_listener.stats.json"
    assert stats_path.exists()


def test_main_entry_once_returns_zero_and_prints_stats(
    fake_server: FakeTgServer,
    host_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:abc")
    monkeypatch.delenv("TELEGRAM_CALLBACK_SECRET", raising=False)
    fake_server.enqueue_updates([])  # one poll, no callbacks

    rc = _main_entry([
        "--root", str(host_root),
        "--once",
        "--quiet",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Stats blob is JSON, so it must contain these keys.
    assert "polls" in out
    assert "dispatched=0" in out


def test_parse_chat_allowlist_handles_mixed_input() -> None:
    """Helper round-trip: numeric IDs become int, channel handles stay str."""
    assert _parse_chat_allowlist(None) is None
    assert _parse_chat_allowlist("") is None
    assert _parse_chat_allowlist("123,456") == [123, 456]
    assert _parse_chat_allowlist("123, @somechannel") == [123, "@somechannel"]


def test_no_secret_warns_on_stderr(
    host_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Operators forgetting to set callback_secret get a loud stderr nudge."""
    _make_listener(host_root, callback_secret=None)
    err = capsys.readouterr().err
    assert "callback_secret not configured" in err


def test_listener_stats_typed_dict_keys() -> None:
    """Sanity: ListenerStats exposes the fields the README contract promises."""
    annotations = ListenerStats.__annotations__
    for key in (
        "started_at",
        "polls",
        "callback_queries_received",
        "actions_dispatched",
        "last_poll_at",
        "last_action_at",
        "errors",
    ):
        assert key in annotations
