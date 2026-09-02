"""Prompt and structured output helpers for the reflector subagent."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..constants import parse_llm_json

logger = logging.getLogger(__name__)

_NOISY_REFLECTION_TOOLS = {
    "load_tool",
    "load_skill",
    "memory_recall",
    "memory_status",
    "search:web_search_exa",
    "search:web_fetch_exa",
    "ouro:get_asset",
    "ouro:get_comments",
    "ouro:search_assets",
    "ouro:get_team_feed",
    "ouro:get_organizations",
    "ouro:get_teams",
    "ouro:get_team",
}

_FRICTION_KINDS = {
    "skill_misled",
    "wasted_steps",
    "user_correction",
    "repeated_work",
    "tool_failure",
    "instruction_conflict",
}
_FRICTION_SEVERITIES = {"low", "med", "high"}


REFLECTOR_PROMPT = """\
You are a memory curator. Given context about recent activity — either a \
conversation with recent messages, or a completed task run with results — \
extract what is worth remembering long-term.

The admission test: store something only if it would change what the agent \
does in a FUTURE conversation or run. If a future model reading the sentence \
cold could not act differently because of it, leave it out.

The retrieval test: for each candidate, name (to yourself) the concrete \
future situation and the memory_recall query that should surface it. If you \
cannot imagine the query a future run would issue, the memory will never be \
found — drop it.

Budget: most runs merit 0–3 candidates; 5 is the hard maximum. An empty \
result is a good result for routine runs. When one decision or finding could \
be phrased several ways, store ONE consolidated sentence, not variants.

Strategy:
- If memory_recall is available, ALWAYS search for existing memories about \
  the current topic first (batch queries in one call). Use the results both \
  to avoid storing duplicates and to collect the IDs of memories your new \
  candidates supersede. When the run updates a plan, priority list, or \
  conclusion that recall surfaced, your candidate must list the old memory \
  in supersedes rather than sit alongside it.

Output ONLY valid JSON matching this schema (no markdown fences):
{
  "candidates": [{"text": "string", "subject_type": "user"|"agent"|"team"|"asset"|"general", "subject_id_hint": "string", "category": "fact"|"direction"|"preference", "basis": "stated"|"inferred"|"observed", "stability": "stable"|"evolving", "strength": "minor"|"normal"|"high", "team_ids": ["uuid from available teams"], "asset_ids": ["uuid"], "verification_hint": "string or empty", "supersedes": ["memory_id from memory_recall results"]}],
  "user_preferences": ["string"],
  "daily_log_entries": [{"team_id": "uuid from available teams", "entry": "string"}],
  "friction": [{"kind": "skill_misled"|"wasted_steps"|"user_correction"|"repeated_work"|"tool_failure"|"instruction_conflict", "skill": "skill name or null", "evidence": "specific process evidence", "severity": "low"|"med"|"high"}]
}

Writing candidates:
- Distilled semantic memory only: durable facts, preferences, or direction. \
  NOT raw observations, conversation mechanics, or task plumbing.
- Each candidate is ONE self-contained sentence in third person. Name the \
  subject explicitly (username, agent name, team, or asset) — never "the \
  user", "this project", or "it". The sentence must make sense read alone, \
  months later, with no surrounding context.
- Prefix time-sensitive facts with the current date from context ("As of \
  2026-07-04, ..."). Durable facts need no date.
- If a fact references an Ouro asset, include its UUID in asset_ids AND use \
  [asset name](asset:<uuid>) links in the text so the fact is self-contained. \
  Otherwise omit asset_ids.
- Produced assets are mandatory: when the run created a dataset, post, file, \
  quest, service, or similar durable asset (see create_* / update_* tool \
  results with a new id), ALWAYS store one candidate naming what it is, why \
  it exists, and which quest/team it belongs to — with the UUID in asset_ids \
  and an asset: link in the text. Future heartbeats must be able to recall \
  that ID without searching. Do not demote these to daily_log_entries only.
- strength: "minor" for peripheral detail, "normal" for most memories, "high" \
  for explicit human guidance and hard-won lessons.
- If entity files provide background, use them to add richer context to facts \
  (e.g. "User prefers X for project Y" instead of just "User prefers X").
- Do NOT store facts that duplicate or closely overlap with existing memories. \
  Two sentences that would lead a future run to the same action are duplicates \
  even if worded differently; keep the more complete one.
- Write in the language the agent operates in (match the run context language); \
  do not mix languages within a sentence.

Do NOT store (common failure modes):
- Run-status snapshots that will be stale within days: "X is deployed but not \
  yet registered", "Y is awaiting execution", "Z is in progress". If the next \
  step matters, store the durable decision or finding instead, or leave it to \
  the daily log.
- The same priority list or agreement restated from multiple angles. One run \
  that settles a 4-item build order yields ONE direction memory listing the \
  order, not one memory per item plus one per participant's endorsement.
- Intermediate reasoning that a final consolidated finding already covers. \
  Store the conclusion; the steps that led there belong in the daily log or a \
  published post.
- Anything fully recoverable from an asset the agent would naturally re-read \
  (schemas, file contents, post text). Store the pointer plus the non-obvious \
  takeaway, not a transcription.
- Inventories of draft coils/routes, tool renames, or "callable via X" facts \
  that duplicate the live COILS index or an always-loaded skill. Prefer skill \
  adoption over storing a second copy in vector memory.

Coil adoption:
- When the run authored or substantially changed a coil under coils/ (or \
  legacy routes/), the work is unfinished until the owning workspace skill \
  (skills/<domain>.md) — or its skills/<domain>-addendum.md extension when \
  the skill is human-authored — prefers run_coil for that job and demotes \
  the old hand-rolled steps. If the run did that skill/addendum patch, you \
  usually need no vector-memory candidate — the skill is the source of truth.
- If the run left a new/changed coil without updating any skill that would \
  teach future heartbeats to call it, store ONE category=direction candidate \
  (strength high, stability evolving) naming the coil(s) and saying which \
  skill (or <name>-addendum.md) must be patched to prefer run_coil(name, …) \
  — so a future run finishes adoption. Do not store stale tool names \
  (e.g. run_route); use run_coil.

Field rules:
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
- supersedes: when a candidate contradicts, reverses, or updates a memory that \
  memory_recall surfaced, list that memory's id here — the system retires those \
  memories when the candidate is stored. Never invent IDs; omit or leave empty \
  when nothing is superseded.

Direction memories:
- Use category direction for durable work-direction guidance from humans \
  or deliberate decisions about what the agent should focus on next. Capture \
  both positive priorities ("spend more time on X") and negative constraints \
  ("stop doing Y", "avoid Z"). Prefer this over a generic observation when the \
  memory should influence future planning or heartbeat focus.
- Only use direction for explicit human guidance, plan feedback, or deliberate \
  planning decisions. Ambient platform discoveries are evidence, not direction; \
  log them as episodes when they are useful future context.
- When a human explicitly bans, prohibits, or reverses something, store that as \
  a direction with strength high and stability stable, and phrase it so it \
  unambiguously supersedes older advice (name the banned thing and the date). \
  List any recalled memories that recommend the now-banned thing in supersedes.

Daily log entries:
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
- When a run interacted with an Ouro asset in a way that future heartbeats should \
  avoid repeating immediately (for example: commenting on it, reviewing it, or \
  deciding to pass on it for now), write one concise daily_log_entries episode with \
  the asset ref and the substance of what happened so a later model can infer \
  "I already touched this recently."
- For heartbeat engagement actions, make each daily log entry specific enough to \
  prevent accidental repetition on the next tick. Include which asset was touched \
  and the gist of the interaction, not generic text like "engaged with community."

Other outputs:
- user_preferences: Communication style, interests, or workflow patterns observed. \
  Only include clear, repeated signals. Omit for task/run reflection.
- friction: Process failures that should be reviewed separately from durable \
  facts. Use concrete evidence from this run, including process signals when \
  supplied. Do NOT turn friction into candidates or user_preferences. Most runs \
  have no friction and should return an empty list. A high step count alone is \
  not friction; identify a specific avoidable problem. Set skill only when a \
  loaded skill contributed to the problem.
- If nothing is worth remembering or reviewing, return empty lists.
- If the run was trivial (e.g. NO_ACTION) and the task/result contains no explicit
  human guidance, return empty lists. NO_ACTION is only an immediate reply
  decision; it must not cause you to discard explicit human work-direction,
  priority, or avoidance guidance contained in the task.

Examples:

Input: user alice says "great — from now on, publish all benchmark results to the eval-lab team, not general."
Candidate: {"text": "As of 2026-07-04, alice wants benchmark results published to the eval-lab team instead of general.", "subject_type": "agent", "subject_id_hint": "self", "category": "direction", "basis": "stated", "stability": "stable", "strength": "high", "team_ids": ["<eval-lab uuid from available teams>"], "asset_ids": [], "verification_hint": "", "supersedes": []}

Input: a run inspected a dataset and found its schema.
Candidate: {"text": "The [alloy-corpus dataset](asset:d3adbeef-0000-0000-0000-000000000000) stores formation energy in eV/atom in the `formation_energy` column.", "subject_type": "asset", "subject_id_hint": "d3adbeef-0000-0000-0000-000000000000", "category": "fact", "basis": "observed", "stability": "evolving", "strength": "normal", "team_ids": [], "asset_ids": ["d3adbeef-0000-0000-0000-000000000000"], "verification_hint": "re-read the dataset schema", "supersedes": []}

Input: a run created dataset 019f5902-b1eb-7794-b3c9-ada8acfe9d36 as the Oliynyk coverage map for quest 019f8012 in team permanent-magnets.
Candidate: {"text": "As of 2026-07-20, the [Oliynyk candidate coverage map](asset:019f5902-b1eb-7794-b3c9-ada8acfe9d36) is the existing-route-run audit for the 24 Oliynyk RE-free PM candidates under quest 019f8012 in team permanent-magnets.", "subject_type": "asset", "subject_id_hint": "019f5902-b1eb-7794-b3c9-ada8acfe9d36", "category": "fact", "basis": "observed", "stability": "stable", "strength": "high", "team_ids": ["<permanent-magnets uuid from available teams>"], "asset_ids": ["019f5902-b1eb-7794-b3c9-ada8acfe9d36"], "verification_hint": "get_asset on the coverage map id", "supersedes": []}

Input: a heartbeat run posted one comment and the result was "commented on the post"; no human guidance appeared.
Candidates: [] — the comment itself is task plumbing that would not change future behavior. Record it as one daily_log_entries episode with the asset link instead.

Input: a run where the agent and a collaborator agreed on a 4-item build priority order, with the collaborator endorsing item 1 as the biggest unblock; memory_recall surfaced an older memory listing a different priority order (id "abc-123").
Candidate (exactly ONE, not one per item or per endorsement): {"text": "As of 2026-07-09, the agreed build priority order is: (1) mCGCNN magnetic moment predictor — hermes called it the biggest unblock, (2) pre-relaxation energy gate, (3) bias-corrected hull route, (4) band structure route.", "subject_type": "agent", "subject_id_hint": "self", "category": "direction", "basis": "stated", "stability": "evolving", "strength": "high", "team_ids": [], "asset_ids": [], "verification_hint": "confirm current priorities with hermes", "supersedes": ["abc-123"]}

When finished, end the turn with a final message containing ONLY the JSON."""


_STRENGTH_WORDS = {"minor": 0.3, "normal": 0.5, "high": 0.8}


def _coerce_strength(value, default: float = 0.5) -> float:
    """Accept the word scale ("minor"|"normal"|"high") or a raw float."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _STRENGTH_WORDS:
            return _STRENGTH_WORDS[word]
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class DailyLogEntry:
    team_id: str = ""
    entry: str = ""


@dataclass
class ReflectionResult:
    facts_to_store: list[dict] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    daily_log_entries: list[DailyLogEntry] = field(default_factory=list)
    friction: list[dict] = field(default_factory=list)


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
    memory_notes: list[str] | None = None,
    *,
    episode_only: bool = False,
    step_count: int | None = None,
    retry_error_count: int | None = None,
    loaded_skill_names: list[str] | None = None,
) -> str:
    """Build the reflector task for a completed run."""
    tools_compact = []
    for tc in tool_summary or []:
        name = tc.get("tool", "")
        if name in _NOISY_REFLECTION_TOOLS:
            continue
        tc_result = str(tc.get("result", ""))[:400]
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

    notes = [str(n).strip() for n in (memory_notes or []) if str(n).strip()]
    notes_block = ""
    if notes:
        notes_block = (
            "\nPreflight memory notes (store if the run succeeded; fill any "
            "<placeholders> from the result / tool calls with real UUIDs and "
            "put them in asset_ids):\n"
            + "\n".join(f"- {note}" for note in notes)
            + "\n"
        )

    episode_block = ""
    if episode_only:
        episode_block = (
            "\nEPISODE-ONLY MODE: Do not store vector-memory candidates "
            "(return candidates: []). Write one concrete daily_log_entries "
            "episode describing what this heartbeat did (or attempted), with "
            "asset links when available. Empty daily_log_entries is only OK "
            "when literally nothing happened.\n"
        )

    process_lines: list[str] = []
    if step_count is not None:
        process_lines.append(f"- Main-agent steps: {max(0, int(step_count))}")
    if retry_error_count is not None:
        process_lines.append(
            f"- Retry/error steps: {max(0, int(retry_error_count))}"
        )
    skill_names = list(
        dict.fromkeys(
            str(name).strip() for name in (loaded_skill_names or []) if str(name).strip()
        )
    )
    if loaded_skill_names is not None:
        process_lines.append(
            "- Loaded skills: " + (", ".join(skill_names) if skill_names else "(none)")
        )
    process_block = ""
    if process_lines:
        process_block = "\nProcess signals:\n" + "\n".join(process_lines) + "\n"

    return (
        "Reflect on this completed run and extract what is worth remembering.\n\n"
        f"Run mode: {run_mode}\n"
        f"Daily log tag: {tag}\n\n"
        "Available teams (use only these IDs in team_ids):\n"
        f"{available_team_text}\n\n"
        f"Task:\n{task[:1500]}\n\n"
        f"Result:\n{str(result)[:2000]}\n\n"
        f"Tool calls:\n{tools_text}\n"
        f"{process_block}"
        f"{notes_block}"
        f"{episode_block}\n"
        "If this run created a durable Ouro asset (dataset, post, file, quest, "
        "service, etc.), store a candidate with its UUID in asset_ids and an "
        "asset: markdown link — future heartbeats must recall that ID without "
        "searching.\n\n"
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
        "plan-review feedback, and replies on direction-proposal posts.\n\n"
        "If this run authored or changed a coil (coils/ or run_coil / publish_route) "
        "without patching the owning workspace skill (or its "
        "<name>-addendum.md when the skill is human-authored) to prefer "
        "run_coil for that job, store one high-strength direction candidate "
        "naming the coil and the skill/addendum that still needs the adoption "
        "update. If the skill or addendum was already patched, skip "
        "inventory-style coil facts."
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

    data = parse_llm_json(text, expect=dict)
    if data is None:
        preview = text[:200].replace("\n", "\\n")
        logger.warning("Failed to parse reflection result | preview=%r", preview)
        return None

    try:
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
                supersedes = fact.get("supersedes") or []
                if not isinstance(supersedes, list):
                    supersedes = []
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
                        "strength": _coerce_strength(
                            fact.get("strength", fact.get("importance", 0.5))
                        ),
                        "verification_hint": str(fact.get("verification_hint") or ""),
                        "supersedes": [str(mid).strip() for mid in supersedes if str(mid).strip()],
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

        friction = []
        raw_friction = data.get("friction", [])
        if isinstance(raw_friction, list):
            for item in raw_friction:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip().lower()
                severity = str(item.get("severity") or "med").strip().lower()
                evidence = " ".join(str(item.get("evidence") or "").split())
                if (
                    kind not in _FRICTION_KINDS
                    or severity not in _FRICTION_SEVERITIES
                    or not evidence
                ):
                    continue
                skill = str(item.get("skill") or "").strip() or None
                friction.append(
                    {
                        "kind": kind,
                        "skill": skill,
                        "evidence": evidence,
                        "severity": severity,
                    }
                )

        return ReflectionResult(
            facts_to_store=facts,
            user_preferences=data.get("user_preferences", []),
            daily_log_entries=daily_entries,
            friction=friction,
        )
    except Exception as e:
        logger.warning(
            "Failed to read reflection JSON structure: %s | keys=%s",
            e,
            sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
        return None
