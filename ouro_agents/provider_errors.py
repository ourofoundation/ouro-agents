"""Shared helpers for provider error detection and chat UX."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

RetryCallback = Callable[[BaseException, float, int], None]

# Persist a chat note once the first backoff is at least this long.
RATE_LIMIT_NOTE_MIN_DELAY_S = 15.0

# OpenRouter often returns Retry-After: 1 while the upstream is still hot.
# Never sleep less than this on a rate-limit retry or we burn attempts hammering.
MIN_RATE_LIMIT_SLEEP_S = 15.0

RATE_LIMIT_NOTE = (
    "Hit a provider rate limit. Retrying, this may take a couple of minutes."
)

RATE_LIMIT_FAIL_MESSAGE = (
    "Couldn't get a response. The model provider is rate-limiting right now. "
    "Try again in a bit."
)

# OpenRouter 402: account can't afford the requested max_tokens / generation.
# Not retryable; credits need topping up (or max_tokens lowered).
CREDIT_FAIL_MESSAGE = (
    "Couldn't get a response. The model provider is out of credits right now. "
    "Try again later."
)

_RETRY_AFTER_RE = re.compile(
    r"retry[_ -]?after(?:_seconds|_seconds_raw)?[\"'\s:=]+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _walk_exception_chain(exc: BaseException):
    """Yield *exc* and each cause/context, without looping."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_rate_limit_error(exc: BaseException) -> bool:
    """Return True when *exc* (or its cause chain) looks like a 429 / rate-limit."""
    return any(_is_rate_limit_error_one(current) for current in _walk_exception_chain(exc))


def _is_rate_limit_error_one(exc: BaseException) -> bool:
    error_str = str(exc).lower()
    if (
        "429" in error_str
        or "rate limit" in error_str
        or "too many requests" in error_str
        or "rate_limit" in error_str
        or "rate-limited" in error_str
    ):
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    return False


def is_credit_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like OpenRouter 402 / insufficient credits.

    Distinct from rate limits: these are not retryable. The operator must add
    credits (or lower max_tokens) before the next request can succeed.
    """
    return any(_is_credit_error_one(current) for current in _walk_exception_chain(exc))


def _is_credit_error_one(exc: BaseException) -> bool:
    error_str = str(exc).lower()
    if (
        "requires more credits" in error_str
        or "insufficient credits" in error_str
        or "can only afford" in error_str
        or "out of credits" in error_str
        or ("402" in error_str and "credit" in error_str)
    ):
        return True
    status = getattr(exc, "status_code", None)
    if status == 402:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 402:
        return True
    # OpenAI SDK PaymentRequiredError / similar — code attribute.
    code = getattr(exc, "code", None)
    if code in (402, "402"):
        return True
    return False


def provider_fail_reply(exc: BaseException) -> Optional[tuple[str, dict[str, Any]]]:
    """Return ``(message, metadata)`` for a known provider failure, else None."""
    if is_credit_error(exc):
        return CREDIT_FAIL_MESSAGE, {"turn_final": True, "credit_exhausted": True}
    if is_rate_limit_error(exc):
        return RATE_LIMIT_FAIL_MESSAGE, {"turn_final": True, "rate_limited": True}
    return None


def parse_retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Extract a Retry-After / retry_after_seconds value from *exc* when present.

    Accepts values in a sane window (1–120s). Returns None when missing or
    outside that window so callers can fall back to computed backoff.
    """
    candidates: list[float] = []

    # Walk cause chain — AgentGenerationError wraps the OpenAI RateLimitError.
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None) if response is not None else None
        if headers is not None:
            raw = None
            try:
                raw = headers.get("retry-after") or headers.get("Retry-After")
            except Exception:
                raw = None
            if raw is not None:
                try:
                    candidates.append(float(raw))
                except (TypeError, ValueError):
                    pass
        for match in _RETRY_AFTER_RE.finditer(str(current)):
            try:
                candidates.append(float(match.group(1)))
            except (TypeError, ValueError):
                continue
        current = current.__cause__ or current.__context__

    for value in candidates:
        if 1.0 <= value <= 120.0:
            return value
    return None


def resolve_retry_delay(computed_delay: float, exc: BaseException) -> float:
    """Blend computed exponential backoff with Retry-After without hammering.

    OpenRouter often advertises ``Retry-After: 1`` while the upstream model is
    still rate-limited for much longer. Prefer a shorter header when it would
    cut a multi-minute sleep, but never sleep less than
    :data:`MIN_RATE_LIMIT_SLEEP_S`.
    """
    computed = max(0.0, float(computed_delay))
    header = parse_retry_after_seconds(exc)
    if header is None:
        return computed if computed > 0 else MIN_RATE_LIMIT_SLEEP_S
    # Shorten huge backoffs with the header, but keep a hammer-prevention floor.
    capped = min(max(header, MIN_RATE_LIMIT_SLEEP_S), computed) if computed > 0 else max(
        header, MIN_RATE_LIMIT_SLEEP_S
    )
    return max(MIN_RATE_LIMIT_SLEEP_S, capped)


def format_rate_limit_activity(model_id: str, delay_s: float) -> str:
    """Human-readable activity label for the chat thinking indicator."""
    model = (model_id or "the model").strip() or "the model"
    if delay_s >= 90:
        minutes = max(1, int(round(delay_s / 60.0)))
        wait = f"~{minutes}m"
    elif delay_s >= 1:
        wait = f"~{int(round(delay_s))}s"
    else:
        wait = "momentarily"
    return f"Rate-limited on {model}, retrying in {wait}..."


class NotifyingRetrying:
    """Wrap a smolagents ``Retrying`` instance to notify before each sleep.

    Also short-circuits delay via :func:`resolve_retry_delay` when the
    exception carries a Retry-After hint.
    """

    def __init__(
        self,
        inner: Any,
        *,
        retry_callback: Optional[RetryCallback] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._inner = inner
        self.retry_callback = retry_callback
        self._sleep = sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __call__(self, fn, *args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        backoff = float(getattr(self._inner, "wait_seconds", 0.0) or 0.0)
        max_attempts = int(getattr(self._inner, "max_attempts", 1) or 1)
        exponential_base = float(getattr(self._inner, "exponential_base", 2.0) or 2.0)
        jitter = bool(getattr(self._inner, "jitter", True))
        retry_predicate = getattr(self._inner, "retry_predicate", None)
        reraise = bool(getattr(self._inner, "reraise", False))
        before_sleep_logger = getattr(self._inner, "before_sleep_logger", None)
        after_logger = getattr(self._inner, "after_logger", None)

        for attempt_number in range(1, max_attempts + 1):
            try:
                result = fn(*args, **kwargs)

                if after_logger and attempt_number > 1:
                    log, log_level = after_logger
                    seconds = time.time() - start_time
                    fn_name = getattr(fn, "__name__", repr(fn))
                    log.log(
                        log_level,
                        f"Finished call to '{fn_name}' after {seconds:.3f}(s), "
                        f"this was attempt n°{attempt_number}/{max_attempts}.",
                    )
                return result

            except BaseException as e:
                should_retry = retry_predicate(e) if retry_predicate else False
                if not should_retry or attempt_number >= max_attempts:
                    if reraise:
                        raise
                    raise

                if after_logger:
                    log, log_level = after_logger
                    seconds = time.time() - start_time
                    fn_name = getattr(fn, "__name__", repr(fn))
                    log.log(
                        log_level,
                        f"Finished call to '{fn_name}' after {seconds:.3f}(s), "
                        f"this was attempt n°{attempt_number}/{max_attempts}.",
                    )

                # Grow the exponential backoff independently of any Retry-After
                # shortening applied to this sleep only.
                backoff *= exponential_base * (1 + jitter * random.random())
                delay = resolve_retry_delay(backoff, e)

                if before_sleep_logger:
                    log, log_level = before_sleep_logger
                    fn_name = getattr(fn, "__name__", repr(fn))
                    log.log(
                        log_level,
                        f"Retrying {fn_name} in {delay} seconds as it raised "
                        f"{e.__class__.__name__}: {e}.",
                    )

                if self.retry_callback is not None:
                    try:
                        self.retry_callback(e, delay, attempt_number)
                    except Exception:
                        logger.warning(
                            "rate-limit retry_callback failed", exc_info=True
                        )

                if delay > 0:
                    self._sleep(delay)
