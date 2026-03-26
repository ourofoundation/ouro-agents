"""Conversation turn persistence, formatting, and history-step building."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from smolagents import ActionStep
from smolagents.memory import TaskStep
from smolagents.monitoring import Timing

logger = logging.getLogger(__name__)


def conversation_file(workspace: Path, conversation_id: str) -> Path:
    conversations_dir = workspace / "conversations"
    conversations_dir.mkdir(parents=True, exist_ok=True)
    return conversations_dir / f"{conversation_id}.jsonl"


def append_conversation_turn(
    workspace: Path,
    conversation_id: str,
    role: str,
    content: str,
    tool_summary: Optional[list[dict]] = None,
) -> None:
    path = conversation_file(workspace, conversation_id)
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "content": content,
    }
    if tool_summary:
        entry["tool_summary"] = tool_summary
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def extract_tool_summary(inner_agent, for_persistence: bool = False) -> list[dict]:
    """Extract tool call information from the inner agent's memory.

    When ``for_persistence`` is True, results are truncated for compact
    JSONL storage.  When False (default), full results are kept so they
    remain available in the current run's context window.
    """
    max_result_chars = 500 if for_persistence else 4000
    summary = []
    for step in inner_agent.memory.steps:
        if not isinstance(step, ActionStep) or not step.tool_calls:
            continue
        for tc in step.tool_calls:
            obs = step.observations or ""
            if len(obs) > max_result_chars:
                obs = obs[:max_result_chars] + "..."
            summary.append({"tool": tc.name, "args": tc.arguments, "result": obs})
    return summary


def load_conversation_turns(
    workspace: Path, conversation_id: str, limit: int = 24
) -> list[dict]:
    path = conversation_file(workspace, conversation_id)
    if not path.exists():
        return []

    turns: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except Exception:
                continue
    return turns[-limit:]


def format_turns_verbatim(turns: list[dict], max_chars: int = 1600) -> str:
    lines = []
    for turn in turns:
        role = str(turn.get("role", "unknown")).lower()
        content = str(turn.get("content", "")).strip()
        if not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars] + "..."
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def compress_tool_call(tc: dict, max_result_chars: int = 600) -> str:
    """Produce a compact summary of a single tool call for history injection."""
    tool_name = tc.get("tool", "unknown")
    args = tc.get("args", {})
    result = str(tc.get("result", ""))

    if tool_name == "final_answer":
        return ""
    if tool_name == "load_tool":
        names = args.get("tool_names", [])
        if isinstance(names, list) and names:
            return f"- Loaded tools: {', '.join(str(n) for n in names)}"
        return "- Loaded tool(s)"
    if tool_name == "memory_store":
        facts = args.get("facts", [])
        if isinstance(facts, list):
            count = len(facts)
            preview = str(facts[0].get("fact", ""))[:80] if facts else ""
            suffix = f" and {count - 1} more" if count > 1 else ""
            return f"- Stored memory: {preview}{suffix}"
        return "- Stored memory"
    if tool_name == "memory_recall":
        queries = args.get("queries", [])
        if isinstance(queries, list):
            query_strs = [
                str(q.get("query", q) if isinstance(q, dict) else q)[:50]
                for q in queries[:3]
            ]
            count = result.count("\n- ") + (1 if result.startswith("- ") else 0)
            return f"- Recalled {count} memories for: {'; '.join(query_strs)}"
        return "- Recalled memories"

    result_preview = result[:max_result_chars]
    if len(result) > max_result_chars:
        result_preview += "..."
    return f"- {tool_name}({json.dumps(args)}) → {result_preview}"


def format_conversation_turns(
    turns: list[dict],
    recent_verbatim: int = 8,
    summarize_fn: Optional[Callable[[list[dict]], str]] = None,
) -> str:
    """Format conversation turns with optional summarization of older turns.

    ``summarize_fn`` is called with the older turns and should return a short
    summary string.  When omitted, a simple length-based fallback is used.
    """
    if not turns:
        return ""

    if len(turns) <= recent_verbatim:
        return format_turns_verbatim(turns)

    old_turns = turns[:-recent_verbatim]
    recent_turns = turns[-recent_verbatim:]

    if summarize_fn:
        summary = summarize_fn(old_turns)
    else:
        condensed = []
        for turn in old_turns:
            content = str(turn.get("content", ""))[:300]
            condensed.append(content)
        blob = " ".join(condensed)
        summary = f"({len(old_turns)} earlier messages about: {blob[:200]}...)"

    recent = format_turns_verbatim(recent_turns)
    return f"Earlier context: {summary}\n\nRecent:\n{recent}"


def build_history_steps(turns: list[dict]) -> list:
    """Convert JSONL conversation turns into smolagents memory steps.

    Pairs user/assistant turns into TaskStep + ActionStep sequences so the
    model sees proper structured conversation history instead of a text blob.
    """
    _DUMMY_TIMING = Timing(start_time=0.0, end_time=0.0)
    steps: list = []
    i = 0
    while i < len(turns):
        turn = turns[i]
        role = turn.get("role", "")
        content = turn.get("content", "")

        if role == "user":
            steps.append(TaskStep(task=content))
            if i + 1 < len(turns) and turns[i + 1].get("role") == "assistant":
                assistant_turn = turns[i + 1]
                assistant_content = assistant_turn.get("content", "")
                tool_summary = assistant_turn.get("tool_summary")

                model_output = assistant_content
                if tool_summary:
                    tool_lines = [compress_tool_call(tc) for tc in tool_summary]
                    tool_lines = [tl for tl in tool_lines if tl]
                    if tool_lines:
                        model_output = (
                            "Tools used:\n"
                            + "\n".join(tool_lines)
                            + "\n\n"
                            + assistant_content
                        )

                steps.append(
                    ActionStep(
                        step_number=len(steps),
                        timing=_DUMMY_TIMING,
                        model_output=model_output,
                        is_final_answer=True,
                    )
                )
                i += 2
                continue
        elif role == "assistant":
            steps.append(
                ActionStep(
                    step_number=len(steps),
                    timing=_DUMMY_TIMING,
                    model_output=content,
                    is_final_answer=True,
                )
            )
        i += 1
    return steps
