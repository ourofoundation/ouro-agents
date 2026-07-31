"""Per-turn context accounting for chat runs.

Chat quality work — history policy, compaction thresholds, prompt-cache
behavior — is guesswork without a record of what actually went into each
turn's prompt. This module measures a chat turn's prompt composition and
history accounting into a :class:`ChatTurnRecord` for the run log.

Measurement is estimate-based on the way in (``chars // CHARS_PER_TOKEN``,
the same approximation ``soul`` uses for its prompt budget) and exact on the
way out, once the provider reports real usage. Comparing the two is the point:
the estimate says what we built, the usage says what it cost and how much of
it the cache actually served.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

from .constants import CHARS_PER_TOKEN
from .run_log import ChatTurnRecord

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Rough token count for budget and telemetry math."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def estimate_turn_tokens(turns: Sequence[dict]) -> int:
    """Estimate the prompt cost of conversation turns injected as memory steps.

    Counts the tool-call summaries that ``build_history_steps`` prefixes onto
    assistant turns, since those are real prompt content.
    """
    total = 0
    for turn in turns:
        total += estimate_tokens(str(turn.get("content", "") or ""))
        tool_summary = turn.get("tool_summary")
        if tool_summary:
            try:
                total += estimate_tokens(json.dumps(tool_summary, default=str))
            except (TypeError, ValueError):
                pass
    return total


def build_chat_turn_record(
    *,
    run_id: str,
    conversation_id: str,
    agent_name: str = "",
    trigger_turn_id: Optional[str] = None,
    model: str = "",
    all_turns: Sequence[dict],
    injected_turns: Sequence[dict],
    history_steps: int,
    system_prompt: str,
    dynamic_context: str,
    task: str,
    compacted: bool = False,
    compaction_reason: Optional[str] = None,
) -> ChatTurnRecord:
    """Measure a chat turn's prompt composition before the model runs."""
    est_system = estimate_tokens(system_prompt)
    est_dynamic = estimate_tokens(dynamic_context)
    est_history = estimate_turn_tokens(injected_turns)
    est_task = estimate_tokens(task)

    return ChatTurnRecord(
        run_id=run_id,
        conversation_id=conversation_id,
        agent_name=agent_name,
        trigger_turn_id=trigger_turn_id,
        model=model,
        turns_fetched=len(all_turns),
        turns_injected=len(injected_turns),
        history_steps=history_steps,
        history_covers_conversation=len(injected_turns) == len(all_turns),
        dropped_oldest_turns=max(0, len(all_turns) - len(injected_turns)),
        est_system_tokens=est_system,
        est_dynamic_tokens=est_dynamic,
        est_history_tokens=est_history,
        est_task_tokens=est_task,
        est_prompt_tokens=est_system + est_dynamic + est_history + est_task,
        compacted=compacted,
        compaction_reason=compaction_reason,
    )


def apply_usage(record: ChatTurnRecord, usage: Any) -> ChatTurnRecord:
    """Fill the record's provider-reported token and cost accounting."""
    if usage is None:
        return record
    try:
        record.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        record.cached_input_tokens = int(getattr(usage, "cached_input_tokens", 0) or 0)
        record.cache_write_tokens = int(getattr(usage, "cache_write_tokens", 0) or 0)
        record.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        record.num_api_calls = int(getattr(usage, "num_api_calls", 0) or 0)
        record.cost_usd = getattr(usage, "cost_usd", None)
        if not record.model:
            record.model = str(getattr(usage, "model_id", "") or "")
    except Exception:
        logger.debug("Failed to read usage into chat turn record", exc_info=True)
    return record


def format_chat_turn(record: ChatTurnRecord) -> str:
    """One-line human-readable summary for the run log output."""
    parts = [
        f"turns={record.turns_injected}/{record.turns_fetched}",
        (
            f"est_prompt={record.est_prompt_tokens}"
            f" (sys={record.est_system_tokens}"
            f" dyn={record.est_dynamic_tokens}"
            f" hist={record.est_history_tokens}"
            f" task={record.est_task_tokens})"
        ),
        f"input={record.input_tokens}",
        f"cached={record.cached_input_tokens} ({record.cache_hit_ratio:.0%})",
        f"cache_write={record.cache_write_tokens}",
        f"output={record.output_tokens}",
    ]
    if record.dropped_oldest_turns:
        parts.append(f"dropped_oldest={record.dropped_oldest_turns}")
    if record.compacted:
        parts.append(f"compacted={record.compaction_reason or 'yes'}")
    return "Chat turn: " + " ".join(parts)
