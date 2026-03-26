"""Prompt and structured output helpers for the reflector subagent."""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


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
- user_preferences: Communication style, interests, or workflow patterns observed. \
  Only include clear, repeated signals. Omit for task/run reflection.
- daily_log_entry: One-line summary of what was accomplished. \
  Link any Ouro assets created or referenced using markdown: [asset name](asset:<uuid>). \
  Use lowercase kebab-case for the activity tag, e.g. [event:comment], [task], [event:mention], [heartbeat]. \
  If the run context says Run mode: heartbeat, use [heartbeat], not [task]. \
  Format: "[tag] brief description with [linked assets](asset:<uuid>)"
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


def build_run_reflection_task(
    task: str,
    result: str,
    tool_summary: list[dict] | None = None,
    run_mode: str = "autonomous",
) -> str:
    """Build the reflector task for a completed run."""
    tools_compact = []
    for tc in (tool_summary or []):
        name = tc.get("tool", "")
        if name == "final_answer":
            continue
        tc_result = str(tc.get("result", ""))[:300]
        tools_compact.append(f"- {name}: {tc_result}")
    tools_text = "\n".join(tools_compact[:10]) if tools_compact else "(no tool calls)"

    return (
        "Reflect on this completed run and extract what is worth remembering.\n\n"
        f"Run mode: {run_mode}\n\n"
        f"Task:\n{task[:600]}\n\n"
        f"Result:\n{str(result)[:800]}\n\n"
        f"Tool calls:\n{tools_text}"
    )


def normalize_daily_log_entry(entry: str, run_mode: str = "autonomous") -> str:
    """Normalize reflector daily-log output for the given run mode."""
    _prefix_overrides = {"heartbeat": "[heartbeat]", "plan": "[planning]", "review": "[review]"}
    override = _prefix_overrides.get(run_mode)
    if override and entry.startswith("[task]"):
        return override + entry[len("[task]"):]
    return entry


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
