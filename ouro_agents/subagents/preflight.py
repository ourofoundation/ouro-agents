"""Prompt and structured output helpers for the preflight subagent."""

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


PREFLIGHT_PROMPT = """\
You are a preflight analyst for an AI agent. Given a user request (and \
optionally conversation context), classify the task and gather any relevant \
context from memory so the agent can start with a clear picture.

Strategy:
- First, classify the intent and complexity of the request.
- Use memory_recall with 1-3 queries (in a single call) depending on complexity.
  Try different angles: the direct topic, related entities, and past decisions/preferences.
- For moderate or complex tasks, synthesize a briefing and sketch a short execution plan.
- For simple tasks, a quick classification and brief memory check is enough.

Output ONLY valid JSON matching this schema (no markdown fences, no explanation):
{
  "intent": "question" | "create" | "analyze" | "research" | "manage" | "converse",
  "complexity": "simple" | "moderate" | "complex",
  "worth_remembering": true | false,
  "briefing": "Synthesized relevant context from memory, or empty string if nothing relevant.",
  "plan": "Numbered execution plan for moderate/complex tasks, or empty string for simple."
}

Rules:
- intent: "question" = asking for info; "create" = producing content; "analyze" = data/computation; \
"research" = web search + synthesis; "manage" = admin/org tasks; "converse" = casual chat
- complexity: "simple" = one step or direct reply; "moderate" = 2-3 tool calls; \
"complex" = multi-step, research + synthesis, or ambiguous scope
- worth_remembering: false for greetings, acknowledgments, trivial follow-ups; true otherwise
- briefing: Lead with the most relevant information. Preserve specific facts, names, IDs, \
and decisions. Drop anything irrelevant. Empty string if no useful memories found.
- plan: Concrete steps the agent can take. Reference specific tools or actions. \
One line per step. Empty string if the task is simple enough to not need a plan.
- Be efficient with memory_recall — batch multiple queries in one call, and don't search if the request is clearly simple/conversational.

When finished, call final_answer with ONLY the JSON."""


HEARTBEAT_PREFLIGHT_PROMPT = """\
You are the preflight analyst for an autonomous heartbeat.
Your job is to decide what the agent should focus on during this heartbeat tick.

You will be provided with the current autonomous playbook and a list of active plans.
Decide whether the agent should:
1. Work on a specific active plan ("work_on_plan").
2. Execute the general playbook ("general_heartbeat").
3. Do nothing / skip this heartbeat ("skip").

You may use memory_recall to check recent context or decisions if it helps you decide, but it is not required.

Assume active plans can span multiple heartbeats. If you choose "work_on_plan",
you are choosing the best next slice of progress for this tick, not asking the
agent to complete the whole plan right now.

Output ONLY valid JSON matching this schema (no markdown fences, no explanation):
{
  "action": "work_on_plan" | "general_heartbeat" | "skip",
  "plan_id": "8-char plan ID if action is work_on_plan, else null",
  "reasoning": "Brief explanation of why you chose this action."
}

When finished, call final_answer with ONLY the JSON."""


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


@dataclass
class HeartbeatPreflightResult:
    action: str = "general_heartbeat"
    plan_id: str | None = None
    reasoning: str = ""


def parse_heartbeat_preflight_result(raw: str) -> HeartbeatPreflightResult:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
        return HeartbeatPreflightResult(
            action=data.get("action", "general_heartbeat"),
            plan_id=data.get("plan_id"),
            reasoning=data.get("reasoning", ""),
        )
    except Exception as e:
        logger.warning("Failed to parse heartbeat preflight result, using defaults: %s", e)
        return HeartbeatPreflightResult()
