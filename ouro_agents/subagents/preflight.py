"""Prompt and structured output helpers for the preflight subagent."""

import logging
import uuid
from dataclasses import dataclass, field

from ..constants import parse_llm_json, strip_markdown_fence

logger = logging.getLogger(__name__)


PREFLIGHT_PROMPT = """\
You are a preflight analyst for an AI agent. Given a user request (and \
optionally conversation context), classify the task and gather any relevant \
context from memory so the agent can start with a clear picture.

Your job is analysis only. Do not execute the user's task, do not draft the \
final user-facing response, and do not perform side effects. Your plan is only \
a launchpad for the main agent's first concrete actions, not a substitute for them. If the task text \
mentions MCP tools such as write_comment, create_post, send_message, execute_route, \
or update_quest, treat those as instructions for the main agent later — never \
call them during preflight.

Strategy:
- First, classify the intent and complexity of the request.
- If the request is self-contained, conversational, or simple enough that memory cannot change the outcome, do not call tools; immediately reply with valid JSON.
- Otherwise, call memory_recall exactly once with 1-3 batched queries. Try different angles: the direct topic, related entities/assets, and past decisions/preferences.
- If memory_recall returns no useful context, still reply with briefing="" and a plan based on the request.
- For moderate or complex tasks, synthesize a briefing and sketch a short execution plan
  that names likely platform actions: MCP tools to load, routes/services/assets to inspect,
  assets to create, or results to verify.
- Prefer plans that create durable requested artifacts, execute existing Ouro routes/services,
  query datasets directly, or inspect real outputs over plans that only produce prose about what
  could be done.
- If a previous response failed or was not accepted, immediately reply with the best valid JSON you can produce.

Finish by ending the turn with a final message that contains no tool calls and is \
ONLY valid JSON matching this schema (no markdown fences, no explanation):
{
  "intent": "question" | "create" | "analyze" | "research" | "manage" | "converse",
  "complexity": "simple" | "moderate" | "complex",
  "worth_remembering": true | false,
  "briefing": "Synthesized relevant context from memory, or empty string if nothing relevant.",
  "plan": "Numbered concrete action plan for moderate/complex tasks, or empty string for simple.",
  "tools": ["server:tool_name", ...]
}

Rules:
- intent: "question" = asking for info; "create" = producing content; "analyze" = data/computation; \
"research" = web search + synthesis; "manage" = admin/org tasks; "converse" = casual chat
- complexity: "simple" = one step or direct reply; "moderate" = 2-3 tool calls; \
"complex" = multi-step, research + synthesis, or ambiguous scope
- worth_remembering: false for greetings, acknowledgments, trivial follow-ups; true otherwise
- briefing: Lead with the most relevant information. Preserve specific facts, names, IDs, \
asset refs, URLs, and decisions. Include a memory only if it would change what the main agent \
does on this task; when unsure, leave it out. Empty string if no useful memories found.
- plan: Concrete steps the main agent can execute with MCP tools. Reference specific MCP \
tools, route/service discovery, asset creation/transformation, or result inspection when relevant. \
One line per step. End moderate/complex plans with a verification step that names the expected \
evidence (asset ID, action ID, dataset rows, status, URL, or exact change). Empty string if the \
task is simple enough to not need a plan.
- tools: MCP tools (exact qualified names from the Available MCP Tools list, when provided) that \
the main agent will very likely call for this task, so they can be preloaded and called without a \
load_tool step. List at most 6, only tools your plan actually uses, most important first. Empty \
list for simple/conversational tasks or when unsure.
- plan ordering: if the request explicitly names a durable artifact to create (a quest, post, \
plan, dataset), put that creation step FIRST — before web research, outreach, or other long \
execution — so the requested deliverable exists even if later steps stall. Do not front-load \
research ahead of an artifact the user directly asked for.
- Be efficient with memory_recall — at most one call, with multiple queries batched into that call.
- The only available preflight tool is memory_recall. Never call side-effecting platform MCP tools.
- Your final message must be the JSON object alone — no surrounding prose."""


HEARTBEAT_PREFLIGHT_PROMPT = """\
You are the heartbeat strategist. You run once per tick on a strong model. \
A cheaper executor will follow your plan exactly — it will not replan.

You receive the full inbox/playbook/direction context. Choose ONE valuable \
objective for this tick and produce an executable brief. Do not perform the \
work yourself. Do not call side-effecting tools. memory_recall is available \
only if you need a missing fact; prefer the context already provided.

Finish with ONLY valid JSON (no markdown fences):
{
  "intent": "create" | "analyze" | "research" | "manage" | "converse",
  "complexity": "simple" | "moderate" | "complex",
  "worth_remembering": true | false,
  "objective": "One concrete objective for this heartbeat.",
  "rationale": "Why this objective now (1-3 sentences).",
  "briefing": "Minimal facts/IDs the executor needs (team_id, quest_id, asset ids, constraints).",
  "plan": "Numbered ordered actions the cheap executor should take.",
  "actions": ["step 1", "step 2"],
  "delegates": [{"subagent": "search|research|writer|executor|developer", "task": "..."}],
  "evidence": ["what proves the tick succeeded"],
  "stop_conditions": ["when to stop even if unfinished"],
  "tools": ["ouro:tool_name", ...],
  "prefetch_assets": ["<uuid>", ...],
  "memory_notes": ["fact to store after a successful tick", ...]
}

Rules:
- Prefer one meaningful slice over finishing an entire multi-step plan.
- prefetch_assets: up to 3 asset UUIDs (quest, dataset, post) whose CONTENT the \
executor will certainly need in its first steps — they are fetched for it up front \
so it does not spend steps on get_asset. Only include IDs you are certain of \
(from the inbox or memory); never guess. Empty list when nothing qualifies.
- memory_notes: up to 4 short fact templates the post-run reflector should store \
if this tick succeeds. Prefer durable pointers future ticks will need — especially \
newly created asset IDs (datasets, posts, quests) with name + purpose + team. Use \
placeholders like <new_dataset_id> when the ID is not known yet; the reflector fills \
them from the run result. Empty list for pass ticks or when nothing lasting is produced.
- memory_recall: at most ONE call, with all queries batched into it. Do not \
spend multiple steps on repeated recalls — you have a hard step budget and must \
still end with the JSON reply.
- If nothing is worth doing, set objective to "pass", plan/actions empty, tools [], \
worth_remembering false, memory_notes [].
- worth_remembering: true only when this tick will produce durable facts worth storing \
(new decisions, durable outcomes, lessons that should change future ticks). false for \
pass ticks, routine status checks, or work with no lasting memory value.
- Routine current-info lookup → delegate to `search` (never tell the executor to load search MCP).
- Substantial multi-source writeup → delegate to `research`.
- Keep tools to Ouro MCP tools the executor will call itself (max 6).
- Preserve exact IDs, URLs, and done-conditions from the inbox.
- Your final message must be the JSON object alone."""


# Cap on preflight-selected tool preloads: enough for a real hot path,
# small enough that a rambling selection can't crowd the tool list.
MAX_PREFLIGHT_TOOLS = 6

# Cap on assets prefetched into the heartbeat executor's context. Asset
# bodies can be large; three is enough for quest + primary input.
MAX_PREFLIGHT_PREFETCH_ASSETS = 3

# Cap on strategist memory-note hints passed to the post-run reflector.
MAX_PREFLIGHT_MEMORY_NOTES = 4

# Heartbeat objectives that mean "do nothing this tick".
_PASS_OBJECTIVES = frozenset(
    {"pass", "nothing", "noop", "no-op", "n/a", "none", "skip"}
)


@dataclass
class PreflightResult:
    """Structured output from the preflight subagent."""

    intent: str = "converse"
    complexity: str = "simple"
    worth_remembering: bool = True
    briefing: str = ""
    plan: str = ""
    tools: list[str] = field(default_factory=list)
    # Heartbeat-specific fields (optional for chat/autonomous preflight).
    objective: str = ""
    rationale: str = ""
    actions: list[str] = field(default_factory=list)
    delegates: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    prefetch_assets: list[str] = field(default_factory=list)
    memory_notes: list[str] = field(default_factory=list)

    @property
    def is_trivial(self) -> bool:
        return self.intent == "converse" and self.complexity == "simple"

    @property
    def has_heartbeat_brief(self) -> bool:
        return bool(self.objective or self.actions or self.plan)

    @property
    def is_pass_objective(self) -> bool:
        return self.objective.strip().lower() in _PASS_OBJECTIVES

    def should_remember(self) -> bool:
        """Whether post-run reflection should run for this preflight result.

        Heartbeat pass/no-op ticks never reflect, even if the model forgets to
        set ``worth_remembering`` false. Otherwise honor the preflight flag.
        """
        if self.is_pass_objective:
            return False
        return bool(self.worth_remembering)


def _parse_tool_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    tools = [str(t).strip() for t in raw if str(t).strip()]
    return list(dict.fromkeys(tools))[:MAX_PREFLIGHT_TOOLS]


def _parse_str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _parse_prefetch_assets(raw: object) -> list[str]:
    """Keep only well-formed UUIDs so a hallucinated id can't poison prefetch."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip().lower()
        try:
            uuid.UUID(text)
        except ValueError:
            continue
        if text not in out:
            out.append(text)
    return out[:MAX_PREFLIGHT_PREFETCH_ASSETS]


def _parse_memory_notes(raw: object) -> list[str]:
    """Short fact templates for the post-run reflector (deduped, capped)."""
    notes = _parse_str_list(raw)
    return list(dict.fromkeys(notes))[:MAX_PREFLIGHT_MEMORY_NOTES]


def _parse_delegate_list(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subagent = str(item.get("subagent", "")).strip()
        task = str(item.get("task", "")).strip()
        if subagent and task:
            out.append({"subagent": subagent, "task": task})
    return out


def _coerce_plan(raw: object) -> str:
    """Accept plan as a string or a list of step strings."""
    if isinstance(raw, list):
        steps = [str(item).strip() for item in raw if str(item).strip()]
        if not steps:
            return ""
        # Number steps if the model returned bare strings.
        numbered = []
        for i, step in enumerate(steps, 1):
            if step[:1].isdigit() and (". " in step[:4] or ") " in step[:4]):
                numbered.append(step)
            else:
                numbered.append(f"{i}. {step}")
        return "\n".join(numbered)
    return str(raw or "")


def parse_preflight_result(raw: str) -> PreflightResult:
    """Parse the JSON output of the preflight subagent into a PreflightResult."""
    text = strip_markdown_fence(raw)
    data = parse_llm_json(raw, expect=dict)
    if isinstance(data, dict):
        try:
            return PreflightResult(
                intent=data.get("intent", "converse"),
                complexity=data.get("complexity", "simple"),
                worth_remembering=data.get("worth_remembering", True),
                briefing=str(data.get("briefing", "") or ""),
                plan=_coerce_plan(data.get("plan", "")),
                tools=_parse_tool_list(data.get("tools")),
                objective=str(data.get("objective", "") or ""),
                rationale=str(data.get("rationale", "") or ""),
                actions=_parse_str_list(data.get("actions")),
                delegates=_parse_delegate_list(data.get("delegates")),
                evidence=_parse_str_list(data.get("evidence")),
                stop_conditions=_parse_str_list(data.get("stop_conditions")),
                prefetch_assets=_parse_prefetch_assets(data.get("prefetch_assets")),
                memory_notes=_parse_memory_notes(data.get("memory_notes")),
            )
        except Exception as e:
            logger.warning("Failed to coerce preflight result, using defaults: %s", e)

    logger.warning("Failed to parse preflight result, using defaults")
    return PreflightResult(briefing=text if text else "")


def format_heartbeat_execution_brief(preflight: PreflightResult) -> str:
    """Compact brief for the cheap heartbeat executor (no full planning context)."""
    if not preflight.has_heartbeat_brief and not preflight.plan:
        return (
            "## Heartbeat Execution Brief\n"
            "Preflight did not return a usable plan. Make one bounded, high-value "
            "platform action if the inbox clearly requires it; otherwise pass.\n"
            "Do not invent a long plan or call search MCP tools directly — "
            "delegate to `search` / `research` when current information is needed."
        )

    parts = ["## Heartbeat Execution Brief"]
    parts.append(
        "Follow this brief from the strong preflight. Do not invent a second plan. "
        "Do not escalate to a stronger model. Prefer the named delegates for "
        "search/research/writing."
    )
    if preflight.objective:
        parts.append(f"### Objective\n{preflight.objective}")
    if preflight.rationale:
        parts.append(f"### Rationale\n{preflight.rationale}")
    if preflight.briefing:
        parts.append(f"### Needed Context\n{preflight.briefing}")
    plan_text = preflight.plan
    if not plan_text and preflight.actions:
        plan_text = "\n".join(
            f"{i}. {step}" for i, step in enumerate(preflight.actions, 1)
        )
    if plan_text:
        parts.append(f"### Ordered Actions\n{plan_text}")
    if preflight.delegates:
        lines = [
            f"- `{d['subagent']}`: {d['task']}" for d in preflight.delegates
        ]
        parts.append("### Suggested Delegates\n" + "\n".join(lines))
    if preflight.evidence:
        parts.append(
            "### Required Evidence\n"
            + "\n".join(f"- {item}" for item in preflight.evidence)
        )
    if preflight.stop_conditions:
        parts.append(
            "### Stop Conditions\n"
            + "\n".join(f"- {item}" for item in preflight.stop_conditions)
        )
    return "\n\n".join(parts)
