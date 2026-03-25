"""Lightweight task classification utilities.

The LLM-based classifier has been replaced by the preflight subagent.
This module retains only the regex-based trivial-message detection and
the PreflightResult dataclass for structured preflight output.
"""

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

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


def is_trivial_message(text: str) -> bool:
    """Return True for greetings, acknowledgments, and other trivial messages."""
    return bool(_TRIVIAL_PATTERNS.match(text.strip()))


@dataclass
class PreflightResult:
    """Structured output from the preflight subagent."""

    intent: str = "converse"
    complexity: str = "simple"
    worth_remembering: bool = True
    briefing: str = ""
    plan: str = ""

    @property
    def is_trivial(self) -> bool:
        return self.intent == "converse" and self.complexity == "simple"


def parse_preflight_result(raw: str) -> PreflightResult:
    """Parse the JSON output of the preflight subagent into a PreflightResult."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
        return PreflightResult(
            intent=data.get("intent", "converse"),
            complexity=data.get("complexity", "simple"),
            worth_remembering=data.get("worth_remembering", True),
            briefing=data.get("briefing", ""),
            plan=data.get("plan", ""),
        )
    except Exception as e:
        logger.warning("Failed to parse preflight result, using defaults: %s", e)
        return PreflightResult(briefing=text if text else "")
