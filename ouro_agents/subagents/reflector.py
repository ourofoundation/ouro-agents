"""Prompt and structured output helpers for the reflector subagent."""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_NOISY_REFLECTION_TOOLS = {
    "load_tool",
    "load_skill",
    "memory_recall",
    "memory_status",
    "search:tavily_search",
    "ouro:get_asset",
    "ouro:get_comments",
    "ouro:search_assets",
    "ouro:get_team_feed",
    "ouro:get_organizations",
    "ouro:get_teams",
    "ouro:get_team",
}


REFLECTOR_PROMPT = """\
You are a memory curator. Given context about recent activity — either a \
conversation with recent messages, or a completed task run with results — \
extract what is worth remembering long-term. Be selective — only include \
things that would be useful in FUTURE conversations or runs.

Strategy:
- If memory_recall is available, search for existing memories about the current \
topic to avoid storing duplicates (batch queries in one call)

Output ONLY valid JSON matching this schema (no markdown fences):
{
  "facts_to_store": [{"text": "string", "category": "fact"|"decision"|"learning"|"observation", "importance": 0.0-1.0, "asset_refs": ["uuid"]}],
  "user_preferences": ["string"],
  "daily_log_entry": "string"
}

Rules:
- facts_to_store: Important facts, decisions, or knowledge gained. NOT conversation \
  mechanics or task plumbing. Assign a category and importance \
  (0.3=minor, 0.5=normal, 0.7=significant, 0.9=critical). \
  If a fact references an Ouro asset, include its UUID in asset_refs AND use \
  [asset name](asset:<uuid>) links in the text so the fact is self-contained. Otherwise omit asset_refs.
- When a run interacted with an Ouro asset in a way that future heartbeats should \
  avoid repeating immediately (for example: commenting on it, reviewing it, or \
  deciding to pass on it for now), prefer storing one concise observation with \
  the asset ref and the substance of what happened so a later model can infer \
  "I already touched this recently."
- user_preferences: Communication style, interests, or workflow patterns observed. \
  Only include clear, repeated signals. Omit for task/run reflection.
- daily_log_entry: One-line summary of what was accomplished. \
  Link any Ouro assets created or referenced using markdown: [asset name](asset:<uuid>). \
  If the run context includes "Daily log tag: [tag]", use that exact tag as the prefix. \
  Otherwise use [chat] for conversation reflections. Never invent tags like [heartbeat] \
  or [event:comment] yourself — the system determines the correct tag. \
  Format: "[tag] brief description with [linked assets](asset:<uuid>)"
- For heartbeat engagement actions, make the daily_log_entry specific enough to \
  prevent accidental repetition on the next tick. Include which asset was touched \
  and the gist of the interaction, not generic text like "engaged with community."
- If nothing is worth remembering, return empty lists and an empty string.
- If the run was trivial (e.g. NO_ACTION), return empty list and empty string.
- Be concise. Each fact/preference should be one sentence.
- Do NOT store facts that duplicate or closely overlap with existing memories.
- If entity files provide background, use them to add richer context to facts \
  (e.g. "User prefers X for project Y" instead of just "User prefers X").

When finished, call final_answer with ONLY the JSON."""


@dataclass
class ReflectionResult:
    facts_to_store: list[dict] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    daily_log_entry: str = ""


_CHAT_EVENT_TYPES = {"new-message", "new-conversation"}


def resolve_daily_log_tag(run_mode: str, event_type: Optional[str] = None) -> str:
    """Compute the deterministic daily-log tag from the run mode and event type."""
    if event_type and event_type not in _CHAT_EVENT_TYPES:
        return f"[event:{event_type}]"
    _mode_tags = {
        "heartbeat": "[heartbeat]",
        "plan": "[planning]",
        "review": "[review]",
        "chat": "[chat]",
        "chat-reply": "[chat]",
    }
    return _mode_tags.get(run_mode, "[task]")


def build_run_reflection_task(
    task: str,
    result: str,
    tool_summary: list[dict] | None = None,
    run_mode: str = "autonomous",
    event_type: Optional[str] = None,
) -> str:
    """Build the reflector task for a completed run."""
    tools_compact = []
    for tc in tool_summary or []:
        name = tc.get("tool", "")
        if name in _NOISY_REFLECTION_TOOLS:
            continue
        tc_result = str(tc.get("result", ""))[:300]
        tools_compact.append(f"- {name}: {tc_result}")
    tools_text = (
        "\n".join(tools_compact) if tools_compact else "(no significant tool calls)"
    )

    tag = resolve_daily_log_tag(run_mode, event_type)

    return (
        "Reflect on this completed run and extract what is worth remembering.\n\n"
        f"Run mode: {run_mode}\n"
        f"Daily log tag: {tag}\n\n"
        f"Task:\n{task[:600]}\n\n"
        f"Result:\n{str(result)[:800]}\n\n"
        f"Tool calls:\n{tools_text}\n\n"
        "If this run commented on, reviewed, or otherwise interacted with an Ouro "
        "asset, capture that interaction concretely so the next heartbeat can tell "
        "the asset was already touched recently and avoid redundant follow-up."
    )


_TAG_RE = re.compile(r"^\[[\w:.-]+\]\s*")


def normalize_daily_log_entry(
    entry: str,
    run_mode: str = "autonomous",
    event_type: Optional[str] = None,
) -> str:
    """Enforce the correct daily-log tag regardless of what the LLM emitted."""
    expected = resolve_daily_log_tag(run_mode, event_type)
    body = _TAG_RE.sub("", entry).strip()
    if not body:
        return entry
    return f"{expected} {body}"


def parse_reflection_result(text: str) -> Optional[ReflectionResult]:
    """Parse an LLM response string into a ReflectionResult."""
    text = text.strip()
    if not text:
        logger.warning("Reflector returned empty output")
        return None
    if text == "Reached max steps.":
        logger.warning("Reflector exhausted its step budget before returning JSON")
        return None
    try:
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)

        facts_raw = data.get("facts_to_store", [])
        facts = []
        for fact in facts_raw:
            if isinstance(fact, str):
                facts.append({"text": fact, "category": "fact", "importance": 0.5})
            elif isinstance(fact, dict):
                facts.append(
                    {
                        "text": fact.get("text", ""),
                        "category": fact.get("category", "fact"),
                        "importance": fact.get("importance", 0.5),
                        "asset_refs": fact.get("asset_refs", []),
                    }
                )

        return ReflectionResult(
            facts_to_store=facts,
            user_preferences=data.get("user_preferences", []),
            daily_log_entry=data.get("daily_log_entry", ""),
        )
    except Exception as e:
        preview = text[:200].replace("\n", "\\n")
        logger.warning("Failed to parse reflection result: %s | preview=%r", e, preview)
        return None
