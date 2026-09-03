"""Conversation turn persistence, formatting, and history-step building."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from smolagents import ActionStep
from smolagents.monitoring import Timing

from ..tools.agent_base import PlainTaskStep

if TYPE_CHECKING:
    from ouro.resources.conversations import Messages

logger = logging.getLogger(__name__)

INTERRUPTED_REPLY_PREFIX = (
    "[The user interrupted this response before it completed. "
    "Treat the request as not fully answered.]"
)

UNANSWERED_USER_MARKER = (
    "[No assistant reply was recorded for this user message "
    "(failed, interrupted, or superseded before a response). "
    "Treat the request as not answered.]"
)

EMPTY_ASSISTANT_REPLY_MARKER = (
    "[The assistant turn produced no reply text "
    "(failed or empty response). Treat the request as not answered.]"
)


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
    from ..memory_lock import memory_write_lock

    path = conversation_file(workspace, conversation_id)
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "content": content,
    }
    if tool_summary:
        entry["tool_summary"] = tool_summary
    with memory_write_lock():
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")


def extract_tool_summary(inner_agent, for_persistence: bool = False) -> list[dict]:
    """Extract tool call information from the inner agent's memory.

    When ``for_persistence`` is True, results are truncated for compact
    JSONL storage.  When False (default), full results are kept so they
    remain available in the current run's context window.

    Parallel step observations are split per ``tool_call_id`` when labeled,
    so each summary entry carries only that call's result.
    """
    from .tool_observations import (
        attribute_observation_results,
        get_step_tool_results,
        tool_call_arguments,
        tool_call_id,
        tool_call_name,
    )

    max_result_chars = 500 if for_persistence else 4000
    summary = []
    for step in inner_agent.memory.steps:
        if not isinstance(step, ActionStep) or not step.tool_calls:
            continue
        results = attribute_observation_results(
            step.tool_calls,
            step.observations or "",
            per_call=get_step_tool_results(step),
        )
        for tc, result in zip(step.tool_calls, results):
            if len(result) > max_result_chars:
                result = result[:max_result_chars] + "..."
            entry = {
                "tool": tool_call_name(tc),
                "args": tool_call_arguments(tc),
                "result": result,
            }
            call_id = tool_call_id(tc)
            if call_id:
                entry["id"] = call_id
            summary.append(entry)
    return summary


def _tool_call_to_dict(tc: Any) -> dict[str, Any]:
    """Normalize a smolagents tool call (dict or object) to ``{name, args, id?}``."""
    from .tool_observations import tool_call_arguments, tool_call_id, tool_call_name

    out: dict[str, Any] = {
        "name": tool_call_name(tc),
        "args": tool_call_arguments(tc),
    }
    call_id = tool_call_id(tc)
    if call_id:
        out["id"] = call_id
    return out


def _step_reasoning(step: Any) -> Optional[str]:
    """Best-effort plain-text reasoning for a memory step, if the provider sent any."""
    from ..provider_reasoning import extract_reasoning_fields

    msg = getattr(step, "model_output_message", None)
    if msg is None:
        return None
    fields = extract_reasoning_fields(msg)
    for key in ("reasoning", "reasoning_content"):
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return val
    details = fields.get("reasoning_details")
    if details:
        try:
            return json.dumps(details, default=str)
        except Exception:
            return str(details)
    return None


def _step_duration(step: Any) -> Optional[float]:
    timing = getattr(step, "timing", None)
    if timing is None:
        return None
    try:
        return timing.duration
    except Exception:
        return None


def extract_run_steps(inner_agent, *, max_observation_chars: int = 0):
    """Structured full-trace snapshot of an inner agent's memory steps.

    Sibling to :func:`extract_tool_summary`, but captures every step type
    (task / planning / action / final) with model output, reasoning, tool
    calls, observations, errors, and timing — for durable run logging.

    ``max_observation_chars`` of 0 means keep observations untruncated.
    """
    from smolagents.memory import PlanningStep, TaskStep

    from ..run_log import RunStepRecord

    def _cap(text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        text = str(text)
        if max_observation_chars and len(text) > max_observation_chars:
            return text[:max_observation_chars] + "..."
        return text

    steps = []
    for step in getattr(inner_agent.memory, "steps", []) or []:
        if isinstance(step, TaskStep):
            steps.append(
                RunStepRecord(
                    step_type="task",
                    model_output=getattr(step, "task", "") or "",
                )
            )
            continue
        if isinstance(step, PlanningStep):
            steps.append(
                RunStepRecord(
                    step_type="planning",
                    model_output=getattr(step, "plan", "") or None,
                    reasoning=_step_reasoning(step),
                    duration_s=_step_duration(step),
                )
            )
            continue
        if isinstance(step, ActionStep):
            tool_calls = [_tool_call_to_dict(tc) for tc in (step.tool_calls or [])]
            is_final = bool(getattr(step, "is_final_answer", False))
            steps.append(
                RunStepRecord(
                    step_number=getattr(step, "step_number", None),
                    step_type="final" if is_final else "action",
                    model_output=getattr(step, "model_output", None) or None,
                    reasoning=_step_reasoning(step),
                    tool_calls=tool_calls,
                    observations=_cap(getattr(step, "observations", None) or None),
                    error=(str(step.error) if getattr(step, "error", None) else None),
                    is_final_answer=is_final,
                    duration_s=_step_duration(step),
                )
            )
            continue
        steps.append(
            RunStepRecord(step_type="other", model_output=repr(step))
        )
    return steps


def _message_attr(message: dict[str, Any], name: str, default: Any = None) -> Any:
    return message.get(name, default)


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    metadata = _message_attr(message, "metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _is_agent_message(message: dict[str, Any], agent_user_id: Optional[str]) -> bool:
    if not agent_user_id:
        return False
    return str(_message_attr(message, "user_id", "")) == agent_user_id


def _is_user_turn_group(
    group: list[dict[str, Any]], agent_user_id: Optional[str]
) -> bool:
    if len(group) != 1:
        return False
    message = group[0]
    return (
        _message_attr(message, "type") == "message"
        and int(_message_attr(message, "seq", 0) or 0) == 0
        and not _is_agent_message(message, agent_user_id)
    )


def _tool_summary_from_group(group: list[dict[str, Any]]) -> Optional[list[dict]]:
    summary: list[dict] = []
    for message in group:
        if _message_attr(message, "type") != "tool_call":
            continue
        payload = _message_attr(message, "json", {})
        if not isinstance(payload, dict):
            continue
        summary.append(
            {
                "tool": payload.get("name", "unknown"),
                "args": payload.get("arguments", {}),
                "result": payload.get("result", ""),
            }
        )
    return summary or None


def _agent_reply_from_group(group: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Pick the agent's final (or interrupted) message from a turn group."""
    candidates = [
        message
        for message in group
        if _message_attr(message, "type") == "message"
        and str(_message_attr(message, "text", "")).strip()
    ]
    if not candidates:
        return None

    def _priority(message: dict[str, Any]) -> tuple[int, int]:
        metadata = _message_metadata(message)
        if metadata.get("interrupted"):
            return (2, int(_message_attr(message, "seq", 0) or 0))
        if metadata.get("turn_final") is True:
            return (1, int(_message_attr(message, "seq", 0) or 0))
        if metadata.get("turn_final") is False:
            return (-1, int(_message_attr(message, "seq", 0) or 0))
        return (0, int(_message_attr(message, "seq", 0) or 0))

    return max(candidates, key=_priority)


def messages_to_turns(
    messages: list[dict[str, Any]],
    *,
    agent_user_id: Optional[str] = None,
    exclude_turn_ids: Optional[set[str]] = None,
    limit: int = 24,
) -> list[dict]:
    """Convert platform ``messages`` rows into JSONL-compatible turn entries."""
    excluded = exclude_turn_ids or set()
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_order: list[str] = []

    for message in messages:
        turn_id = str(_message_attr(message, "turn_id") or _message_attr(message, "id", ""))
        if not turn_id or turn_id in excluded:
            continue
        if turn_id not in grouped:
            grouped[turn_id] = []
            group_order.append(turn_id)
        grouped[turn_id].append(message)

    turns: list[dict] = []
    for turn_id in group_order:
        group = grouped[turn_id]
        if _is_user_turn_group(group, agent_user_id):
            message = group[0]
            turns.append(
                {
                    "role": "user",
                    "content": str(_message_attr(message, "text", "")).strip(),
                    "timestamp": _message_attr(message, "created_at"),
                    "turn_id": turn_id,
                }
            )
            continue

        if not any(_is_agent_message(message, agent_user_id) for message in group):
            continue

        reply = _agent_reply_from_group(group)
        if reply is None:
            continue

        reply_meta = _message_metadata(reply)
        turns.append(
            {
                "role": "assistant",
                "content": str(_message_attr(reply, "text", "")).strip(),
                "timestamp": _message_attr(reply, "created_at"),
                "turn_id": turn_id,
                "tool_summary": _tool_summary_from_group(group),
                "interrupted": bool(reply_meta.get("interrupted")),
            }
        )

    return turns[-limit:]


def load_conversation_turns_from_db(
    ouro_client,
    conversation_id: str,
    *,
    agent_user_id: Optional[str] = None,
    exclude_turn_ids: Optional[set[str]] = None,
    limit: int = 24,
) -> list[dict]:
    """Load recent chat turns from the platform messages table."""
    from ouro.resources.conversations import Messages

    fetch_limit = min(200, max(limit * 3, 60))
    raw_messages = Messages(ouro_client).list(conversation_id, limit=fetch_limit)
    if not raw_messages:
        return []
    return messages_to_turns(
        list(raw_messages),
        agent_user_id=agent_user_id,
        exclude_turn_ids=exclude_turn_ids,
        limit=limit,
    )


def resolve_conversation_turns(
    workspace: Path,
    conversation_id: str,
    *,
    ouro_client=None,
    agent_user_id: Optional[str] = None,
    exclude_turn_ids: Optional[set[str]] = None,
    limit: int = 24,
) -> list[dict]:
    """Load turns from the DB when possible, else fall back to local JSONL."""
    if ouro_client and conversation_id:
        try:
            return load_conversation_turns_from_db(
                ouro_client,
                conversation_id,
                agent_user_id=agent_user_id,
                exclude_turn_ids=exclude_turn_ids,
                limit=limit,
            )
        except Exception:
            logger.warning(
                "Failed to load conversation turns from DB for %s",
                conversation_id,
                exc_info=True,
            )
    return load_conversation_turns(workspace, conversation_id, limit=limit)


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

    if tool_name in {"final_answer", "no_action"}:
        return ""
    if tool_name == "load_tool":
        names = args.get("tool_names", [])
        if isinstance(names, list) and names:
            return f"- Loaded tools: {', '.join(str(n) for n in names)}"
        return "- Loaded tool(s)"
    if tool_name == "remember":
        memories = args.get("memories", [])
        if isinstance(memories, list) and memories:
            count = len(memories)
            first = memories[0] if isinstance(memories[0], dict) else {}
            preview = str(first.get("text", ""))[:80]
            suffix = f" and {count - 1} more" if count > 1 else ""
            return f"- Stored memory: {preview}{suffix}"
        return "- Stored memory"
    if tool_name == "forget":
        items = args.get("items", [])
        if isinstance(items, dict) and isinstance(items.get("memory_ids"), list):
            count = len(items["memory_ids"])
            return f"- Forgot {count} memor{'ies' if count != 1 else 'y'}"
        if isinstance(items, list) and items:
            count = len(items)
            return f"- Forgot {count} memor{'ies' if count != 1 else 'y'}"
        return "- Forgot memory"
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


# Verbatim history window: at least MIN turns are always shown, and the
# window start only moves once every STEP turns. A plain "last N" window
# shifts by one on every turn, which changes the first history message and
# busts the prompt-prefix cache immediately after the system prompt for the
# rest of the conversation. Anchoring the start index keeps the replayed
# history byte-stable between moves, so the cache only misses once per STEP
# turns while the window size oscillates between MIN and MIN + STEP - 1.
HISTORY_WINDOW_MIN = 8
HISTORY_WINDOW_STEP = 8

# Upper bound on turns fetched for injection. Beyond this, watermark
# compaction (chat_compaction) should already have folded older turns into
# an internal continuity summary — the fetch window is the verbatim tail.
HISTORY_FETCH_LIMIT = 64


def select_history_window(turns: list[dict]) -> list[dict]:
    """Return the cache-friendly verbatim suffix of ``turns``."""
    n = len(turns)
    if n <= HISTORY_WINDOW_MIN:
        return turns
    anchor = HISTORY_WINDOW_STEP * ((n - HISTORY_WINDOW_MIN) // HISTORY_WINDOW_STEP)
    return turns[anchor:]


def _assistant_history_output(assistant_turn: dict) -> str:
    """Build the model_output string for an assistant history step."""
    assistant_content = str(assistant_turn.get("content") or "").strip()
    tool_summary = assistant_turn.get("tool_summary")
    interrupted = bool(assistant_turn.get("interrupted"))

    if not assistant_content:
        if interrupted:
            assistant_content = INTERRUPTED_REPLY_PREFIX
        else:
            assistant_content = EMPTY_ASSISTANT_REPLY_MARKER

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
    return model_output


def build_history_steps(turns: list[dict], *, summary: str = "") -> list:
    """Convert JSONL conversation turns into smolagents memory steps.

    Pairs user/assistant turns into TaskStep + ActionStep sequences so the
    model sees proper structured conversation history instead of a text blob.

    Unanswered user turns (no following assistant reply) get an explicit
    marker step so silence is not misread as an answered gap.

    When ``summary`` is set (from watermark compaction), it is injected as a
    leading synthetic exchange before the verbatim tail.
    """
    _DUMMY_TIMING = Timing(start_time=0.0, end_time=0.0)
    steps: list = []

    if summary and summary.strip():
        steps.append(
            PlainTaskStep(
                task=(
                    "[Internal continuity summary of earlier turns in this "
                    "conversation — not a user message.]"
                )
            )
        )
        steps.append(
            ActionStep(
                step_number=len(steps),
                timing=_DUMMY_TIMING,
                model_output=summary.strip(),
                is_final_answer=True,
            )
        )

    i = 0
    while i < len(turns):
        turn = turns[i]
        role = turn.get("role", "")
        content = turn.get("content", "")

        if role == "user":
            steps.append(PlainTaskStep(task=content))
            if i + 1 < len(turns) and turns[i + 1].get("role") == "assistant":
                steps.append(
                    ActionStep(
                        step_number=len(steps),
                        timing=_DUMMY_TIMING,
                        model_output=_assistant_history_output(turns[i + 1]),
                        is_final_answer=True,
                    )
                )
                i += 2
                continue
            # Unpaired user turn — failed / interrupted / superseded with no reply.
            steps.append(
                ActionStep(
                    step_number=len(steps),
                    timing=_DUMMY_TIMING,
                    model_output=UNANSWERED_USER_MARKER,
                    is_final_answer=True,
                )
            )
        elif role == "assistant":
            steps.append(
                ActionStep(
                    step_number=len(steps),
                    timing=_DUMMY_TIMING,
                    model_output=_assistant_history_output(turn),
                    is_final_answer=True,
                )
            )
        i += 1
    return steps
