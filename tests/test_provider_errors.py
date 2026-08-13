"""Tests for provider rate-limit / credit detection, retry UX, and chat fail messaging."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ouro_agents.config import RunMode
from ouro_agents.events import EventRunContext
from ouro_agents.provider_errors import (
    CREDIT_FAIL_MESSAGE,
    RATE_LIMIT_FAIL_MESSAGE,
    RATE_LIMIT_NOTE,
    RATE_LIMIT_NOTE_MIN_DELAY_S,
    NotifyingRetrying,
    format_rate_limit_activity,
    is_credit_error,
    is_rate_limit_error,
    is_transient_provider_error,
    parse_retry_after_seconds,
    provider_fail_reply,
    resolve_retry_delay,
)
from ouro_agents.server import (
    _persist_provider_fail_comment,
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


def test_is_transient_provider_error_retries_json_and_transport():
    import json

    assert is_transient_provider_error(
        json.JSONDecodeError("Expecting value", "x", 0)
    )
    assert is_transient_provider_error(
        Exception("Expecting value: line 8327 column 1 (char 45793)")
    )
    assert is_transient_provider_error(Exception("502 Bad Gateway"))
    assert is_transient_provider_error(Exception("Error code: 429 - rate limited"))

    class APIConnectionError(Exception):
        pass

    assert is_transient_provider_error(APIConnectionError("connection reset"))

    # Credit errors stay non-retryable even if message mentions gateway noise.
    assert not is_transient_provider_error(
        Exception("Error code: 402 - requires more credits")
    )
    assert not is_transient_provider_error(Exception("invalid api key"))


def test_notifying_retryer_retries_json_decode_error():
    import json

    sleeps: list[float] = []
    callbacks: list[tuple[float, int]] = []

    class _Inner:
        max_attempts = 3
        wait_seconds = 2.0
        exponential_base = 2.0
        jitter = False
        reraise = True
        before_sleep_logger = None
        after_logger = None
        retry_predicate = staticmethod(is_transient_provider_error)

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return "ok"

    retryer = NotifyingRetrying(
        _Inner(),
        retry_callback=lambda exc, delay, attempt: callbacks.append((delay, attempt)),
        sleep=sleeps.append,
    )
    assert retryer(flaky) == "ok"
    assert attempts["n"] == 3
    assert len(callbacks) == 2
    # Non-rate-limit transient: no 15s floor; first delay is wait*base = 4s.
    assert callbacks[0][0] == 4.0
    assert sleeps == [4.0, 8.0]


def test_is_credit_error_detects_402_and_message():
    openrouter_402 = Exception(
        "Error code: 402 - {'error': {'message': 'This request requires more credits, or "
        "fewer max_tokens. You requested up to 65536 tokens, but can only afford 13639.', "
        "'code': 402}}"
    )
    assert is_credit_error(openrouter_402)
    assert is_credit_error(Exception("Error code: 402 - insufficient credits"))
    assert is_credit_error(Exception("can only afford 100 tokens"))
    assert not is_credit_error(Exception("timeout connecting"))
    assert not is_credit_error(Exception("Error code: 429 - rate limited"))

    exc = Exception("payment required")
    exc.status_code = 402  # type: ignore[attr-defined]
    assert is_credit_error(exc)

    # Credit errors must not be mistaken for retryable rate limits.
    assert not is_rate_limit_error(openrouter_402)


def test_provider_fail_reply_prefers_credit_over_rate_limit():
    credit = provider_fail_reply(Exception("Error code: 402 - requires more credits"))
    assert credit is not None
    assert credit[0] == CREDIT_FAIL_MESSAGE
    assert credit[1]["credit_exhausted"] is True

    rate = provider_fail_reply(Exception("Error code: 429 - rate limited"))
    assert rate is not None
    assert rate[0] == RATE_LIMIT_FAIL_MESSAGE
    assert rate[1]["rate_limited"] is True

    assert provider_fail_reply(Exception("timeout")) is None


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


def test_is_credit_error_walks_cause_chain():
    root = Exception("Error code: 402 - requires more credits")
    wrapped = RuntimeError("generator didn't stop after throw()")
    wrapped.__cause__ = root
    assert is_credit_error(wrapped)


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
    assert model.retryer._inner.retry_predicate is is_transient_provider_error

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


def _event_run(**overrides) -> EventRunContext:
    base = dict(
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
    base.update(overrides)
    return EventRunContext(**base)


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


def test_persist_credit_fail_comment():
    publisher = MagicMock()
    content = SimpleNamespace(text=CREDIT_FAIL_MESSAGE, json={"type": "doc"})
    with (
        patch("ouro_agents.server.reply_publisher", publisher),
        patch("ouro_agents.server.content_from_markdown", return_value=content) as md,
    ):
        _persist_provider_fail_comment(
            _event_run(
                event_type="comment",
                conversation_id=None,
                reply_parent_id="parent-1",
                mode=RunMode.AUTONOMOUS,
            ),
            text=CREDIT_FAIL_MESSAGE,
        )
    md.assert_called_once_with(publisher.client, CREDIT_FAIL_MESSAGE)
    publisher.client.comments.create.assert_called_once_with(
        content=content, parent_id="parent-1"
    )


def test_realtime_session_propagates_body_errors():
    """Body exceptions must not be swallowed into a second yield."""
    from ouro_agents.publisher import OuroReplyPublisher

    publisher = OuroReplyPublisher(client=MagicMock())
    publisher.client.websocket.ensure_connected.return_value = None

    with pytest.raises(RuntimeError, match="boom"):
        with publisher.realtime_session():
            raise RuntimeError("boom")

    publisher.client.websocket.session.assert_not_called()
    publisher.client.websocket.disconnect.assert_not_called()


def test_realtime_session_falls_back_when_open_fails():
    from ouro_agents.publisher import OuroReplyPublisher

    publisher = OuroReplyPublisher(client=MagicMock())
    publisher.client.websocket.ensure_connected.side_effect = ConnectionError("down")

    ran = {"ok": False}
    with publisher.realtime_session():
        ran["ok"] = True
    assert ran["ok"] is True
