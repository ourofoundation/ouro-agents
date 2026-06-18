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
  "candidates": [{"text": "string", "subject_type": "user"|"agent"|"team"|"asset"|"general", "subject_id_hint": "string", "category": "fact"|"direction"|"preference", "basis": "stated"|"inferred"|"observed", "stability": "stable"|"evolving", "strength": 0.3|0.5|0.8, "team_ids": ["uuid from available teams"], "asset_ids": ["uuid"], "verification_hint": "string or empty"}],
  "user_preferences": ["string"],
  "daily_log_entries": [{"team_id": "uuid from available teams", "entry": "string"}]
}

Rules:
- candidates: Distilled semantic memory only: durable facts, preferences, or \
  direction. NOT raw observations, conversation mechanics, or task plumbing. \
  Assign a coarse strength seed (0.3=minor, 0.5=normal, 0.8=high). \
  If a fact references an Ouro asset, include its UUID in asset_ids AND use \
  [asset name](asset:<uuid>) links in the text so the fact is self-contained. Otherwise omit asset_ids.
- basis: Use stated for explicit human instructions/preferences/facts, observed \
  for tool or platform evidence, inferred for the agent's synthesis.
- stability: stable means the memory is durable until contradicted; evolving \
  means it may become stale with time or changing system state.
- verification_hint: For evolving facts, optionally include a brief \
  hint about how the fact could be re-verified in the future. Examples: \
  "check GET /api/endpoint", "search for asset by name", "query team feed". \
  Leave empty for stable facts or facts that can only be verified by human confirmation.
- team_ids is per-candidate. Use only IDs listed in the Available teams block. \
  If no listed team applies, return an empty list. Do not invent team IDs.
- subject_type answers what the memory is about: user preferences are user, \
  agent operating learnings/directions are agent, team-specific project knowledge \
  is team, asset interactions are asset, and broadly applicable facts are general.
- direction: Use this category for durable work-direction guidance from humans \
  or deliberate decisions about what the agent should focus on next. Capture \
  both positive priorities ("spend more time on X") and negative constraints \
  ("stop doing Y", "avoid Z"). Prefer this over a generic observation when the \
  memory should influence future planning or heartbeat focus.
- Only use direction for explicit human guidance, plan feedback, or deliberate \
  planning decisions. Ambient platform discoveries are evidence, not direction; \
  log them as episodes when they are useful future context.
- When a human explicitly bans, prohibits, or reverses something, store that as \
  a direction with strength 0.8 and stability stable, and phrase it so it \
  unambiguously supersedes older advice (name the banned thing and the date). \
  If memory_recall surfaced existing memories that recommend the now-banned \
  thing, mention them in the candidate text so consolidation can remove them.
- When a run interacted with an Ouro asset in a way that future heartbeats should \
  avoid repeating immediately (for example: commenting on it, reviewing it, or \
  deciding to pass on it for now), write one concise daily_log_entries episode with \
  the asset ref and the substance of what happened so a later model can infer \
  "I already touched this recently."
- user_preferences: Communication style, interests, or workflow patterns observed. \
  Only include clear, repeated signals. Omit for task/run reflection.
- daily_log_entries: Team-specific one-line summaries of what was accomplished. \
  Use this for episodic memory: what happened, when, and which assets were touched. \
  Use this when a run did work relevant to one or more listed teams. Each entry \
  must describe only the work relevant to its team_id; do not copy one generic \
  entry across teams. Use only IDs from the Available teams block. If no listed \
  team applies, return an empty list. Link any Ouro assets created or referenced \
  using markdown: [asset name](asset:<uuid>). \
  If the run context includes "Daily log tag: [tag]", use that exact tag as the prefix. \
  Otherwise use [chat] for conversation reflections. Never invent tags like [heartbeat] \
  or [event:comment] yourself — the system determines the correct tag. \
  Format: "[tag] brief description with [linked assets](asset:<uuid>)"
- For heartbeat engagement actions, make each daily log entry specific enough to \
  prevent accidental repetition on the next tick. Include which asset was touched \
  and the gist of the interaction, not generic text like "engaged with community."
- If nothing is worth remembering, return empty lists.
- If the run was trivial (e.g. NO_ACTION) and the task/result contains no explicit
  human guidance, return empty lists. NO_ACTION is only an immediate reply
  decision; it must not cause you to discard explicit human work-direction,
  priority, or avoidance guidance contained in the task.
- Be concise. Each fact/preference should be one sentence.
- Do NOT store facts that duplicate or closely overlap with existing memories.
- If entity files provide background, use them to add richer context to facts \
  (e.g. "User prefers X for project Y" instead of just "User prefers X").

When finished, end the turn with a final message containing ONLY the JSON."""


@dataclass
class DailyLogEntry:
    team_id: str = ""
    entry: str = ""


@dataclass
class ReflectionResult:
    facts_to_store: list[dict] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    daily_log_entries: list[DailyLogEntry] = field(default_factory=list)


def resolve_daily_log_tag(
    run_mode: str,
    event_type: Optional[str] = None,
    team_name: str = "",
) -> str:
    """Compute the deterministic daily-log tag from the run mode and event type.

    When *team_name* is provided the tag is suffixed: ``[heartbeat:research]``.
    """
    from ouro_agents.event_registry import is_chat_event

    if event_type and not is_chat_event(event_type):
        base = f"event:{event_type}"
    else:
        _mode_tags = {
            "heartbeat": "heartbeat",
            "plan": "planning",
            "review": "review",
            "chat": "chat",
        }
        base = _mode_tags.get(run_mode, "task")

    if team_name:
        return f"[{base}:{team_name}]"
    return f"[{base}]"


def build_run_reflection_task(
    task: str,
    result: str,
    tool_summary: list[dict] | None = None,
    run_mode: str = "autonomous",
    event_type: Optional[str] = None,
    team_name: str = "",
    available_teams: list[dict] | None = None,
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

    tag = resolve_daily_log_tag(run_mode, event_type, team_name=team_name)
    team_lines: list[str] = []
    for team in available_teams or []:
        team_id = team.get("id") or ""
        if not team_id:
            continue
        slug = team.get("slug") or ""
        name = team.get("name") or ""
        team_lines.append(f"- {team_id} · {slug} · {name}")
    available_team_text = "\n".join(team_lines) if team_lines else "- (none; leave team_ids = [])"

    return (
        "Reflect on this completed run and extract what is worth remembering.\n\n"
        f"Run mode: {run_mode}\n"
        f"Daily log tag: {tag}\n\n"
        "Available teams (use only these IDs in team_ids):\n"
        f"{available_team_text}\n\n"
        f"Task:\n{task[:600]}\n\n"
        f"Result:\n{str(result)[:800]}\n\n"
        f"Tool calls:\n{tools_text}\n\n"
        "If this run commented on, reviewed, or otherwise interacted with an Ouro "
        "asset, capture that interaction concretely so the next heartbeat can tell "
        "the asset was already touched recently and avoid redundant follow-up.\n\n"
        "For daily_log_entries, write separate entries for separate teams only when "
        "the work was actually relevant to each team. Do not broadcast one generic "
        "summary to multiple teams.\n\n"
        "If the task or result includes human guidance about what the agent should "
        "work on, avoid, prioritize, de-prioritize, or change in future plans, store "
        'that as a category=\"direction\" memory even when the run result is '
        "NO_ACTION. This is especially important for comments, mentions, "
        "plan-review feedback, and replies on direction-proposal posts."
    )


_TAG_RE = re.compile(r"^\[[\w:.-]+\]\s*")


def normalize_daily_log_entry(
    entry: str,
    run_mode: str = "autonomous",
    event_type: Optional[str] = None,
    team_name: str = "",
) -> str:
    """Enforce the correct daily-log tag regardless of what the LLM emitted."""
    expected = resolve_daily_log_tag(run_mode, event_type, team_name=team_name)
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

        facts_raw = data.get("candidates", data.get("facts_to_store", []))
        facts = []
        for fact in facts_raw:
            if isinstance(fact, str):
                facts.append({"text": fact, "category": "fact", "strength": 0.5})
            elif isinstance(fact, dict):
                category = fact.get("category", "fact")
                subject_type = fact.get("subject_type")
                if not subject_type:
                    subject_type = (
                        "agent"
                        if category
                        in {"learning", "decision", "direction", "observation", "episode"}
                        else "user"
                    )
                asset_ids = fact.get("asset_ids", fact.get("asset_refs", []))
                facts.append(
                    {
                        "text": fact.get("text", ""),
                        "subject_type": subject_type,
                        "subject_id_hint": fact.get("subject_id_hint", fact.get("subject_id", "")),
                        "category": category,
                        "basis": fact.get("basis", "inferred"),
                        "stability": fact.get("stability", "stable"),
                        "team_ids": fact.get("team_ids", []),
                        "asset_ids": asset_ids,
                        "strength": fact.get("strength", fact.get("importance", 0.5)),
                        "verification_hint": str(fact.get("verification_hint") or ""),
                        "asset_refs": asset_ids,
                    }
                )

        daily_entries = []
        for entry in data.get("daily_log_entries", []):
            if not isinstance(entry, dict):
                continue
            team_id = str(entry.get("team_id") or "").strip()
            if not team_id:
                team_ids = entry.get("team_ids")
                if isinstance(team_ids, list) and team_ids:
                    team_id = str(team_ids[0] or "").strip()
            text = str(
                entry.get("entry")
                or entry.get("text")
                or ""
            ).strip()
            if text:
                daily_entries.append(DailyLogEntry(team_id=team_id, entry=text))

        return ReflectionResult(
            facts_to_store=facts,
            user_preferences=data.get("user_preferences", []),
            daily_log_entries=daily_entries,
        )
    except Exception as e:
        preview = text[:200].replace("\n", "\\n")
        logger.warning("Failed to parse reflection result: %s | preview=%r", e, preview)
        return None
