"""Shared constants and lightweight utilities used across the ouro-agents package."""

import json
import re
from typing import Any, Optional

CHARS_PER_TOKEN = 4
"""Rough estimate of characters per token for budget calculations."""

GLOBAL_ORG_UUID = "00000000-0000-0000-0000-000000000000"
"""Ouro global (personal) organization id when no specific org is set."""

FETCHABLE_ASSET_TYPES = frozenset(
    {"post", "comment", "file", "dataset", "service", "route"}
)
"""Asset types that can be retrieved via ouro.assets.retrieve / get_asset."""

_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")
_INTERVAL_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval_seconds(interval: str) -> Optional[int]:
    """Parse an interval shorthand like '4h' or '30m' to seconds.

    Returns None for unrecognised formats (e.g. cron expressions).
    """
    m = _INTERVAL_RE.match(interval.strip())
    if not m:
        return None
    return int(m.group(1)) * _INTERVAL_MULTIPLIERS[m.group(2)]


_JSON_FENCE_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def parse_json_from_llm(text: str) -> Optional[dict[str, Any]]:
    """Extract JSON from an LLM response that may be wrapped in a markdown fence.

    Tries ``json`` fenced block first, then falls back to parsing the raw text.
    Returns None if both attempts fail.
    """
    fence = _JSON_FENCE_RE.search(text)
    raw = fence.group(1) if fence else text
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None
