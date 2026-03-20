import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from smolagents import OpenAIModel

logger = logging.getLogger(__name__)

INTENTS = [
    "question",    # asking for information, no side effects
    "create",      # producing new content (post, dataset, file)
    "analyze",     # data exploration, computation, research
    "research",    # web search + synthesis
    "manage",      # admin tasks: notifications, team ops, file org
    "converse",    # casual chat, greetings, follow-ups
]

SKILL_NAMES = ["ouro", "python", "filesystem", "web-search"]

MCP_SERVERS = ["ouro", "filesystem", "search"]


@dataclass
class TaskClassification:
    intent: str = "converse"
    complexity: str = "simple"          # simple | moderate | complex
    needs_planning: bool = False
    relevant_skills: list[str] = field(default_factory=list)
    relevant_servers: list[str] = field(default_factory=list)
    worth_remembering: bool = True

    @property
    def is_trivial(self) -> bool:
        return self.intent == "converse" and self.complexity == "simple"


CLASSIFY_PROMPT = f"""\
You are a task classifier for an AI agent. Given a user message, output a JSON object with these fields:

- "intent": one of {json.dumps(INTENTS)}
- "complexity": "simple" | "moderate" | "complex"
- "needs_planning": true if the task has multiple distinct steps or ambiguity that benefits from a plan
- "relevant_skills": subset of {json.dumps(SKILL_NAMES)} the agent will likely need
- "relevant_servers": subset of {json.dumps(MCP_SERVERS)} whose tools the agent will likely call
- "worth_remembering": false for greetings, acknowledgments, trivial follow-ups; true otherwise

Rules:
- "simple" = answerable in one step or a direct reply
- "moderate" = 2-3 tool calls, straightforward
- "complex" = multi-step, research + synthesis, or ambiguous scope
- When unsure, lean toward including more skills/servers rather than fewer
- Output ONLY valid JSON, no markdown fences, no explanation
"""


def classify_task(
    task: str,
    model: OpenAIModel,
    conversation_summary: Optional[str] = None,
) -> TaskClassification:
    """Classify a task using a cheap LLM call before full execution."""
    user_content = f"Task: {task}"
    if conversation_summary:
        user_content = f"Conversation context: {conversation_summary}\n\n{user_content}"

    try:
        result = model(
            [
                {"role": "system", "content": CLASSIFY_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        data = json.loads(text)
        return TaskClassification(
            intent=data.get("intent", "converse"),
            complexity=data.get("complexity", "simple"),
            needs_planning=data.get("needs_planning", False),
            relevant_skills=[s for s in data.get("relevant_skills", []) if s in SKILL_NAMES],
            relevant_servers=[s for s in data.get("relevant_servers", []) if s in MCP_SERVERS],
            worth_remembering=data.get("worth_remembering", True),
        )
    except Exception as e:
        logger.warning("Task classification failed, using defaults: %s", e)
        return TaskClassification(
            relevant_skills=list(SKILL_NAMES),
            relevant_servers=list(MCP_SERVERS),
        )
