"""Shared constants and lightweight utilities used across the ouro-agents package."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

CHARS_PER_TOKEN = 4
"""Rough estimate of characters per token for budget calculations."""

GLOBAL_ORG_UUID = "00000000-0000-0000-0000-000000000000"
"""Ouro global (personal) organization id when no specific org is set."""

FETCHABLE_ASSET_TYPES = frozenset(
    {"post", "comment", "file", "dataset", "service", "route"}
)
"""Asset types that can be retrieved via ouro.assets.retrieve / get_asset."""

# OpenRouter app attribution — https://openrouter.ai/docs/app-attribution
# HTTP-Referer is required for rankings; title/categories are optional.
DEFAULT_OPENROUTER_HTTP_REFERER = "https://ouro.foundation"
DEFAULT_OPENROUTER_APP_TITLE = "Ouro"
DEFAULT_OPENROUTER_APP_CATEGORIES = "personal-agent,cloud-agent"


def openrouter_attribution_headers() -> dict[str, str]:
    """Headers that attribute OpenRouter usage to the Ouro app.

    Override via ``OPENROUTER_HTTP_REFERER``, ``OPENROUTER_APP_TITLE``, and
    ``OPENROUTER_APP_CATEGORIES`` when needed (e.g. local ranking experiments).
    """
    headers = {
        "HTTP-Referer": os.getenv(
            "OPENROUTER_HTTP_REFERER", DEFAULT_OPENROUTER_HTTP_REFERER
        ),
        "X-OpenRouter-Title": os.getenv(
            "OPENROUTER_APP_TITLE", DEFAULT_OPENROUTER_APP_TITLE
        ),
    }
    categories = os.getenv(
        "OPENROUTER_APP_CATEGORIES", DEFAULT_OPENROUTER_APP_CATEGORIES
    ).strip()
    if categories:
        headers["X-OpenRouter-Categories"] = categories
    return headers

_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")
_INTERVAL_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_REL_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_REL_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_interval_seconds(interval: str) -> Optional[int]:
    """Parse an interval shorthand like '4h' or '30m' to seconds.

    Returns None for unrecognised formats (e.g. cron expressions).
    Does not accept weeks; use :func:`parse_relative_timedelta` for CLI ``since``.
    """
    m = _INTERVAL_RE.match(interval.strip())
    if not m:
        return None
    return int(m.group(1)) * _INTERVAL_MULTIPLIERS[m.group(2)]


def parse_relative_timedelta(spec: str) -> Optional[timedelta]:
    """Parse a relative duration like ``24h`` / ``7d`` / ``2w`` into a timedelta."""
    match = _REL_RE.match(spec)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(**{_REL_UNITS[unit]: amount})


def parse_since_datetime(since: Optional[str]) -> Optional[datetime]:
    """Parse a ``since`` bound as a timezone-aware UTC datetime.

    Accepts relative shorthands (``24h``, ``7d``, ``2w``) or ISO-8601 timestamps.
    """
    if not since:
        return None
    delta = parse_relative_timedelta(since)
    if delta is not None:
        return datetime.now(timezone.utc) - delta
    parsed = datetime.fromisoformat(since)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_since_iso(since: Optional[str]) -> Optional[str]:
    """Parse a ``since`` bound to an absolute ISO-8601 string.

    Relative shorthands are resolved against UTC now; other values pass through.
    """
    if not since:
        return None
    delta = parse_relative_timedelta(since)
    if delta is not None:
        return (datetime.now(timezone.utc) - delta).isoformat()
    return since


def clip_text(
    text: object,
    max_len: int,
    *,
    flatten: bool = True,
    ellipsis: str = "…",
) -> str:
    """Flatten whitespace (optional) and truncate ``text`` to ``max_len`` chars."""
    s = str(text or "")
    if flatten:
        s = " ".join(s.split())
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    keep = max(0, max_len - len(ellipsis))
    return s[:keep] + ellipsis


_JSON_FENCE_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def strip_markdown_fence(text: str) -> str:
    """Strip a leading markdown code fence (``` or ```json) from ``text``."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    return text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()


def extract_balanced_json_object(text: str) -> Optional[str]:
    """Return the first balanced top-level ``{...}`` object in ``text``.

    Models sometimes wrap required JSON in analysis prose. Scan for the first
    ``{`` and walk forward tracking brace depth (ignoring braces inside strings).
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_balanced_json_array(text: str) -> Optional[str]:
    """Return the first balanced top-level ``[...]`` array in ``text``."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_llm_json(
    text: str,
    *,
    expect: type = dict,
) -> Any | None:
    """Parse JSON from an LLM response, tolerating fences and prose wrappers.

    ``expect`` should be ``dict`` or ``list``. Returns ``None`` if parsing fails
    or the top-level value is not of the expected type.
    """
    if not text or not str(text).strip():
        return None

    candidate = strip_markdown_fence(str(text))
    # Prefer an explicit ```json ... ``` match when present (legacy path).
    fence = _JSON_FENCE_RE.search(str(text))
    if fence:
        candidate = fence.group(1).strip()

    try:
        data = json.loads(candidate)
        return data if isinstance(data, expect) else None
    except (json.JSONDecodeError, ValueError):
        pass

    if expect is dict:
        extracted = extract_balanced_json_object(candidate)
    elif expect is list:
        extracted = extract_balanced_json_array(candidate)
    else:
        return None
    if not extracted:
        return None
    try:
        data = json.loads(extracted)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, expect) else None


def parse_json_from_llm(text: str) -> Optional[dict[str, Any]]:
    """Extract a JSON object from an LLM response (fence + prose tolerant).

    Thin wrapper around :func:`parse_llm_json` for callers that expect a dict.
    """
    result = parse_llm_json(text, expect=dict)
    return result if isinstance(result, dict) else None
