"""Tests for system-prompt budget enforcement and section trimming."""

from ouro_agents.constants import CHARS_PER_TOKEN
from ouro_agents.soul import (
    SYSTEM_PROMPT_TOKEN_BUDGET,
    _enforce_budget,
    _trim_section,
)


def test_trim_keeps_head_by_default():
    text = "\n".join(f"line {i}" for i in range(100))
    trimmed = _trim_section(text, 200, keep_tail=False)
    assert trimmed.startswith("line 0")
    assert trimmed.endswith("[...truncated]")
    assert len(trimmed) <= 200 + len("\n[...truncated]")


def test_trim_keeps_tail_preserves_heading_and_recent_lines():
    lines = [f"- turn {i}" for i in range(200)]
    text = "## RECENT CONVERSATION\n" + "\n".join(lines)
    trimmed = _trim_section(text, 400, keep_tail=True)
    assert trimmed.startswith("## RECENT CONVERSATION\n[...truncated]\n")
    assert trimmed.endswith("- turn 199")
    assert "- turn 0" not in trimmed


def test_trim_noop_when_within_limit():
    assert _trim_section("short", 100, keep_tail=False) == "short"
    assert _trim_section("short", 100, keep_tail=True) == "short"


def test_enforce_budget_trims_conversation_from_the_head():
    filler = "x" * (SYSTEM_PROMPT_TOKEN_BUDGET * CHARS_PER_TOKEN)
    conversation = (
        "## RECENT CONVERSATION (most recent last)\n"
        + "\n".join(f"- user: message {i}" for i in range(500))
    )
    sections = {"soul": filler, "conversation": conversation}
    _enforce_budget(sections, ["soul", "conversation"])
    assert sections["soul"] == filler  # protected, untouched
    assert sections["conversation"].endswith("- user: message 499")
    assert "[...truncated]" in sections["conversation"]


def test_enforce_budget_noop_under_budget():
    sections = {"soul": "identity", "conversation": "- user: hi"}
    _enforce_budget(sections, ["soul", "conversation"])
    assert sections["conversation"] == "- user: hi"
