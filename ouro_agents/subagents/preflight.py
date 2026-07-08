"""Prompt and structured output helpers for the preflight subagent."""

import json
import logging
from dataclasses import dataclass, field

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


# Cap on preflight-selected tool preloads: enough for a real hot path,
# small enough that a rambling selection can't crowd the tool list.
MAX_PREFLIGHT_TOOLS = 6


@dataclass
class PreflightResult:
    """Structured output from the preflight subagent."""

    intent: str = "converse"
    complexity: str = "simple"
    worth_remembering: bool = True
    briefing: str = ""
    plan: str = ""
    tools: list[str] = field(default_factory=list)

    @property
    def is_trivial(self) -> bool:
        return self.intent == "converse" and self.complexity == "simple"


def _parse_tool_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    tools = [str(t).strip() for t in raw if str(t).strip()]
    return list(dict.fromkeys(tools))[:MAX_PREFLIGHT_TOOLS]


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
            tools=_parse_tool_list(data.get("tools")),
        )
    except Exception as e:
        logger.warning("Failed to parse preflight result, using defaults: %s", e)
        return PreflightResult(briefing=text if text else "")
