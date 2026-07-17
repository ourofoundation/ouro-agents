"""Tests for provider rate-limit detection, retry UX, and chat fail messaging."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ouro_agents.config import RunMode
from ouro_agents.events import EventRunContext
from ouro_agents.rate_limit import (
    RATE_LIMIT_FAIL_MESSAGE,
    RATE_LIMIT_NOTE,
    RATE_LIMIT_NOTE_MIN_DELAY_S,
    NotifyingRetrying,
    format_rate_limit_activity,
    is_rate_limit_error,
    parse_retry_after_seconds,
    resolve_retry_delay,
)
from ouro_agents.server import (
    ServerAgentObserver,
    _persist_rate_limit_fail_message,
)
from ouro_agents.usage import TrackedOpenAIModel


def test_is_rate_limit_error_detects_429_and_message():
    assert is_rate_limit_error(Exception("Error code: 429 - rate limited"))
    assert is_rate_limit_error(Exception("Provider returned error rate-limited upstream"))
    assert not is_rate_limit_error(Exception("timeout connecting"))

    exc = Exception("boom")
    exc.status_code = 429  # type: ignore[attr-defined]
    assert is_rate_limit_error(exc)


def test_parse_retry_after_from_message_and_headers():
    exc = Exception(
        "{'raw': 'rate-limited', 'retry_after_seconds': 1, 'headers': {'Retry-After': '1'}}"
    )
    assert parse_retry_after_seconds(exc) == 1.0

    headers = {"retry-after": "12"}
    response = SimpleNamespace(headers=headers, status_code=429)
    wrapped = Exception("429")
    wrapped.response = response  # type: ignore[attr-defined]
    assert parse_retry_after_seconds(wrapped) == 12.0

    # Outside sane window → None
    assert parse_retry_after_seconds(Exception("retry_after_seconds: 999")) is None


def test_resolve_retry_delay_prefers_shorter_header():
    exc = Exception("retry_after_seconds: 1")
    # 1s Retry-After must not hammer — floor at MIN_RATE_LIMIT_SLEEP_S.
    assert resolve_retry_delay(205.0, exc) == 15.0
    assert resolve_retry_delay(205.0, Exception("retry_after_seconds: 60")) == 60.0
    assert resolve_retry_delay(0.0, Exception("no header")) == 15.0


def test_is_rate_limit_error_walks_cause_chain():
    root = Exception("Error code: 429 - rate-limited upstream")
    wrapped = RuntimeError("generator didn't stop after throw()")
    wrapped.__cause__ = root
    assert is_rate_limit_error(wrapped)


def test_format_rate_limit_activity():
    assert "kimi-k3" in format_rate_limit_activity("moonshotai/kimi-k3", 205)
    assert "~3m" in format_rate_limit_activity("moonshotai/kimi-k3", 205)
    assert "~20s" in format_rate_limit_activity("x", 20)


def test_notifying_retryer_invokes_callback_and_respects_retry_after():
    sleeps: list[float] = []
    callbacks: list[tuple[float, int]] = []

    class _Inner:
        max_attempts = 3
        wait_seconds = 60.0
        exponential_base = 2.0
        jitter = False
        reraise = True
        before_sleep_logger = None
        after_logger = None
        retry_predicate = staticmethod(is_rate_limit_error)

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception("429 rate limit retry_after_seconds: 1")
        return "ok"

    retryer = NotifyingRetrying(
        _Inner(),
        retry_callback=lambda exc, delay, attempt: callbacks.append((delay, attempt)),
        sleep=sleeps.append,
    )
    assert retryer(flaky) == "ok"
    assert len(callbacks) == 2
    # Header says 1s but floor keeps us from hammering.
    assert callbacks[0][0] == 15.0
    assert sleeps == [15.0, 15.0]


def test_notifying_retryer_reraises_when_exhausted():
    class _Inner:
        max_attempts = 2
        wait_seconds = 1.0
        exponential_base = 2.0
        jitter = False
        reraise = True
        before_sleep_logger = None
        after_logger = None
        retry_predicate = staticmethod(is_rate_limit_error)

    def always_fail():
        raise Exception("429 rate limit")

    retryer = NotifyingRetrying(_Inner(), sleep=lambda _d: None)
    with pytest.raises(Exception, match="429"):
        retryer(always_fail)


def test_tracked_openai_model_disables_sdk_retries_and_wraps_retryer():
    with patch("openai.OpenAI") as openai_cls:
        openai_cls.return_value = MagicMock()
        model = TrackedOpenAIModel(
            model_id="test/model",
            api_base="https://example.test/v1",
            api_key="sk-test",
        )
    assert model.client_kwargs.get("max_retries") == 0
    assert isinstance(model.retryer, NotifyingRetrying)

    seen: list[float] = []
    model.retry_callback = lambda _e, delay, _a: seen.append(delay)
    assert model.retryer.retry_callback is not None

    # Drive the wrapped retryer directly.
    model.retryer._inner.max_attempts = 2
    model.retryer._inner.wait_seconds = 1.0
    model.retryer._inner.exponential_base = 2.0
    model.retryer._inner.jitter = False
    model.retryer._inner.reraise = True
    model.retryer._inner.retry_predicate = is_rate_limit_error
    model.retryer._sleep = lambda _d: None

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("429")
        return "ok"

    assert model.retryer(flaky) == "ok"
    assert len(seen) == 1


def _event_run() -> EventRunContext:
    return EventRunContext(
        event_type="new-message",
        task="hello",
        conversation_id="conv-1",
        user_id="user-1",
        team_id=None,
        actor_user_id="agent-1",
        actor_username="hermes",
        mode=RunMode.CHAT,
        asset_id=None,
        asset_type=None,
        root_asset_id=None,
        root_asset_type=None,
        preload_tools=[],
        prefetch=None,
        provenance=None,
        trigger_turn_id=None,
    )


def test_rate_limit_note_not_promoted_as_final_answer():
    publisher = MagicMock()
    observer = ServerAgentObserver(
        _event_run(),
        stream_message_id="stream-1",
        turn_id="turn-1",
        reply_publisher=publisher,
    )
    observer._intermediate_messages.append(
        {"id": "note-1", "text": RATE_LIMIT_NOTE, "msg": {}}
    )
    observer._intermediate_messages.append(
        {
            "id": "real-1",
            "text": "Here is a real substantial intermediate reply for the user.",
            "msg": {},
        }
    )
    last = observer._last_substantial_intermediate()
    assert last is not None
    assert last["id"] == "real-1"

    observer._intermediate_messages = [
        {"id": "note-1", "text": RATE_LIMIT_NOTE, "msg": {}}
    ]
    assert observer._last_substantial_intermediate() is None


def test_chat_retry_callback_emits_activity_and_one_note():
    """Mirror the agent wiring: activity every time, note once for long delays."""
    observer = MagicMock()
    rate_limit_note_sent = False
    model_label = "moonshotai/kimi-k3"

    def on_provider_retry(exc, delay_s, attempt):
        nonlocal rate_limit_note_sent
        observer.on_activity(
            "thinking", format_rate_limit_activity(model_label, delay_s), True
        )
        if delay_s >= RATE_LIMIT_NOTE_MIN_DELAY_S and not rate_limit_note_sent:
            rate_limit_note_sent = True
            observer.on_intermediate_end("note-id", RATE_LIMIT_NOTE)

    on_provider_retry(Exception("429"), 20.0, 1)
    on_provider_retry(Exception("429"), 40.0, 2)
    on_provider_retry(Exception("429"), 5.0, 3)

    assert observer.on_activity.call_count == 3
    assert observer.on_intermediate_end.call_count == 1
    observer.on_intermediate_end.assert_called_once_with("note-id", RATE_LIMIT_NOTE)


def test_persist_rate_limit_fail_message():
    publisher = MagicMock()
    publisher.client.user.id = "agent-1"
    content = SimpleNamespace(text=RATE_LIMIT_FAIL_MESSAGE, json={"type": "doc"})
    with (
        patch("ouro_agents.server.reply_publisher", publisher),
        patch(
            "ouro_agents.server.content_from_markdown", return_value=content
        ) as md,
        patch("ouro_agents.server.Messages") as messages_cls,
    ):
        messages_cls.return_value.create.return_value = {"id": "msg-1"}
        result = _persist_rate_limit_fail_message(
            _event_run(),
            turn_id="turn-1",
            message_id="stream-1",
            seq=1,
        )
    assert result == {"id": "msg-1"}
    md.assert_called_once()
    _args, kwargs = messages_cls.return_value.create.call_args
    assert kwargs["metadata"]["rate_limited"] is True
    assert kwargs["metadata"]["turn_final"] is True
    assert kwargs["text"] == RATE_LIMIT_FAIL_MESSAGE


def test_realtime_session_propagates_body_errors():
    """Body exceptions must not be swallowed into a second yield."""
    from ouro_agents.publisher import OuroReplyPublisher

    publisher = OuroReplyPublisher(client=MagicMock())
    session_cm = MagicMock()
    session_cm.__enter__ = MagicMock(return_value=None)
    session_cm.__exit__ = MagicMock(return_value=False)
    publisher.client.websocket.session.return_value = session_cm

    with pytest.raises(RuntimeError, match="boom"):
        with publisher.realtime_session():
            raise RuntimeError("boom")

    # Must not have fallen through to a second yield / generator error.
    session_cm.__exit__.assert_called_once()


def test_realtime_session_falls_back_when_open_fails():
    from ouro_agents.publisher import OuroReplyPublisher

    publisher = OuroReplyPublisher(client=MagicMock())
    publisher.client.websocket.session.side_effect = ConnectionError("down")

    ran = {"ok": False}
    with publisher.realtime_session():
        ran["ok"] = True
    assert ran["ok"] is True
