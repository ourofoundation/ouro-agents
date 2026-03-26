"""Lightweight task classification utilities.

The LLM-based classifier has been replaced by the preflight subagent.
This module now retains only regex-based trivial-message detection plus
compatibility re-exports for preflight parsing types.
"""

import re
from typing import Optional

from .subagents.preflight import PreflightResult, parse_preflight_result

_TRIVIAL_PATTERNS = re.compile(
    r"^("
    r"h(i|ey|ello|owdy|ola)"
    r"|yo\b"
    r"|sup\b"
    r"|thanks?( you)?\.?"
    r"|thank(s| you)( so much)?!?"
    r"|ty\b"
    r"|ok(ay)?\.?"
    r"|sure\.?"
    r"|got it\.?"
    r"|cool\.?"
    r"|nice\.?"
    r"|great\.?"
    r"|awesome\.?"
    r"|perfect\.?"
    r"|sounds good\.?"
    r"|good morning\.?"
    r"|good afternoon\.?"
    r"|good evening\.?"
    r"|good night\.?"
    r"|gm\b"
    r"|gn\b"
    r"|bye\.?"
    r"|goodbye\.?"
    r"|see ya\.?"
    r"|lgtm\.?"
    r"|np\.?"
    r"|no worries\.?"
    r"|nvm\.?"
    r"|never\s*mind\.?"
    r"|👋|🙏|👍|😊"
    r")$",
    re.IGNORECASE,
)


def is_trivial_message(text: Optional[str]) -> bool:
    """Return True for greetings, acknowledgments, and other trivial messages."""
    if text is None:
        return False
    return bool(_TRIVIAL_PATTERNS.match(text.strip()))
