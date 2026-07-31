"""Watermark-anchored chat history compaction.

Append-only chat history grows a cache-friendly prefix until the prompt
approaches the model context limit. Compaction then folds older turns into an
internal continuity summary and records a watermark: subsequent runs inject
``summary + turns after watermark``, and the tail grows append-only again.

Compaction is internal-only — the summarize exchange is never posted as an
Ouro chat message. Soft compaction runs after a successful reply (background);
hard compaction runs synchronously before a reply when the prompt would not fit.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .chat_telemetry import estimate_tokens, estimate_turn_tokens
from .constants import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

COMPACTION_FILENAME_SUFFIX = ".compaction.json"

COMPACTION_SYSTEM_PROMPT = """\
You are compacting an earlier portion of a chat for continuity. Write a factual
continuity summary the agent will use instead of the verbatim turns.

Rules:
- Capture what the user asked for, what was decided, key entities/assets, open
  threads, what was done, and what remains.
- Prefer quoting or paraphrasing the user's own wording for intent.
- Do NOT invent standing goals. Ambiguous pivots stay as open threads, not as
  goal replacements. Do not promote the assistant's inferences into durable
  "Active goals" the user did not clearly state.
- Be concise but specific (names, asset ids, concrete decisions).
- Output plain prose (or short bullets). No markdown fences, no preamble.
"""

_compaction_locks: dict[str, threading.Lock] = {}
_compaction_locks_guard = threading.Lock()


def _lock_for(conversation_id: str) -> threading.Lock:
    with _compaction_locks_guard:
        lock = _compaction_locks.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _compaction_locks[conversation_id] = lock
        return lock


@dataclass
class CompactionRecord:
    """Append-only snapshot of a chat compaction."""

    watermark_turn_id: str
    summary: str
    created_at: str = ""
    model: str = ""
    turns_compacted: int = 0
    reason: str = ""
    # Optional prior summary folded into this one (for audit / debugging).
    prior_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompactionRecord":
        return cls(
            watermark_turn_id=str(data.get("watermark_turn_id") or ""),
            summary=str(data.get("summary") or ""),
            created_at=str(data.get("created_at") or ""),
            model=str(data.get("model") or ""),
            turns_compacted=int(data.get("turns_compacted") or 0),
            reason=str(data.get("reason") or ""),
            prior_summary=str(data.get("prior_summary") or ""),
        )


def compaction_path(workspace: Path, conversation_id: str) -> Path:
    return workspace / "conversations" / f"{conversation_id}{COMPACTION_FILENAME_SUFFIX}"


def load_compaction(
    workspace: Path, conversation_id: str
) -> Optional[CompactionRecord]:
    path = compaction_path(workspace, conversation_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return None
        record = CompactionRecord.from_dict(data)
        if not record.watermark_turn_id or not record.summary.strip():
            return None
        return record
    except Exception:
        logger.warning(
            "Failed to load chat compaction for %s", conversation_id, exc_info=True
        )
        return None


def save_compaction(
    workspace: Path, conversation_id: str, record: CompactionRecord
) -> None:
    from .memory_lock import memory_write_lock

    path = compaction_path(workspace, conversation_id)
    with memory_write_lock():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not record.created_at:
            record.created_at = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")


def turn_stable_id(turn: dict, index: int) -> str:
    """Stable id for watermarking. Prefer platform turn_id; else fingerprint."""
    explicit = turn.get("turn_id") or turn.get("id")
    if explicit:
        return str(explicit)
    role = str(turn.get("role") or "")
    ts = str(turn.get("timestamp") or "")
    content = str(turn.get("content") or "")
    # Content prefix keeps fingerprints unique across identical timestamps.
    return f"idx:{index}:{role}:{ts}:{content[:48]}"


def find_watermark_index(
    turns: Sequence[dict], watermark_turn_id: str
) -> Optional[int]:
    """Index of the watermark turn in ``turns``, or None if not found."""
    if not watermark_turn_id:
        return None
    for i, turn in enumerate(turns):
        if turn_stable_id(turn, i) == watermark_turn_id:
            return i
    return None


def split_at_watermark(
    turns: Sequence[dict], record: CompactionRecord
) -> tuple[list[dict], list[dict]]:
    """Split into (compacted_prefix, tail_after_watermark)."""
    idx = find_watermark_index(turns, record.watermark_turn_id)
    if idx is None:
        # Watermark missing (fetch window slipped past it). Treat all fetched
        # turns as the tail — the summary still covers older context.
        return [], list(turns)
    return list(turns[: idx + 1]), list(turns[idx + 1 :])


@dataclass
class HistoryBuildResult:
    """What to inject into agent memory for a chat turn."""

    injected_turns: list[dict] = field(default_factory=list)
    summary: str = ""
    compacted: bool = False
    compaction_reason: Optional[str] = None
    # Turns that were folded into the summary (for telemetry / soft triggers).
    compacted_prefix: list[dict] = field(default_factory=list)


def build_injectable_history(
    all_turns: Sequence[dict],
    *,
    compaction: Optional[CompactionRecord] = None,
) -> HistoryBuildResult:
    """Apply a compaction record to produce the inject set + optional summary."""
    if not compaction or not compaction.summary.strip():
        return HistoryBuildResult(injected_turns=list(all_turns))

    prefix, tail = split_at_watermark(all_turns, compaction)
    return HistoryBuildResult(
        injected_turns=tail,
        summary=compaction.summary.strip(),
        compacted=True,
        compaction_reason=compaction.reason or "watermark",
        compacted_prefix=prefix,
    )


def estimate_chat_prompt_tokens(
    *,
    system_prompt: str,
    dynamic_context: str,
    task: str,
    injected_turns: Sequence[dict],
    summary: str = "",
) -> int:
    total = estimate_tokens(system_prompt)
    total += estimate_tokens(dynamic_context)
    total += estimate_tokens(task)
    total += estimate_turn_tokens(injected_turns)
    if summary:
        total += estimate_tokens(summary)
    return total


def should_compact(
    est_prompt_tokens: int,
    *,
    context_tokens: int,
    soft_fraction: float,
    hard_fraction: float,
) -> Optional[str]:
    """Return ``\"hard\"``, ``\"soft\"``, or None."""
    if context_tokens <= 0:
        return None
    hard = int(context_tokens * hard_fraction)
    soft = int(context_tokens * soft_fraction)
    if est_prompt_tokens >= hard:
        return "hard"
    if est_prompt_tokens >= soft:
        return "soft"
    return None


def format_turns_for_summarizer(
    turns: Sequence[dict], *, max_chars_per_turn: int = 800
) -> str:
    lines: list[str] = []
    for turn in turns:
        role = str(turn.get("role") or "unknown").lower()
        content = str(turn.get("content") or "").strip()
        if not content:
            content = "(empty)"
        if len(content) > max_chars_per_turn:
            content = content[:max_chars_per_turn] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def summarize_turns_for_compaction(
    turns: Sequence[dict],
    model,
    *,
    prior_summary: str = "",
) -> str:
    """Ask ``model`` for a continuity summary of ``turns`` (+ optional prior)."""
    if not turns and not prior_summary.strip():
        return ""

    parts: list[str] = []
    if prior_summary.strip():
        parts.append(f"Prior continuity summary:\n{prior_summary.strip()}")
    if turns:
        parts.append(
            "Turns to fold into the continuity summary:\n"
            + format_turns_for_summarizer(turns)
        )
    user_content = "\n\n".join(parts)

    result = model(
        [
            {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
    )
    text = result.content if hasattr(result, "content") else str(result)
    return str(text).strip()


def compact_history(
    turns: Sequence[dict],
    model,
    *,
    reason: str,
    prior: Optional[CompactionRecord] = None,
    keep_recent: int = 0,
    model_id: str = "",
) -> Optional[CompactionRecord]:
    """Compact ``turns`` into a new :class:`CompactionRecord`.

    When ``keep_recent`` > 0, the last N turns stay out of the summary (they
    remain the verbatim tail). ``prior``'s summary is folded in when present.
    """
    turns_list = list(turns)
    if prior and prior.summary.strip():
        # Only compact the tail after the prior watermark; the prior summary
        # already covers older turns.
        _, after = split_at_watermark(turns_list, prior)
        to_fold_source = after
        prior_summary = prior.summary
    else:
        to_fold_source = turns_list
        prior_summary = ""

    if keep_recent > 0:
        if len(to_fold_source) <= keep_recent:
            return None
        to_fold = to_fold_source[:-keep_recent]
    else:
        to_fold = to_fold_source

    if not to_fold:
        return None

    try:
        summary = summarize_turns_for_compaction(
            to_fold, model, prior_summary=prior_summary
        )
    except Exception:
        logger.warning("Chat history compaction LLM call failed", exc_info=True)
        return None

    if not summary.strip():
        return None

    last = to_fold[-1]
    # Index in the full turns_list for a stable id.
    last_index = turns_list.index(last) if last in turns_list else len(turns_list) - 1
    watermark = turn_stable_id(last, last_index)

    return CompactionRecord(
        watermark_turn_id=watermark,
        summary=summary.strip(),
        created_at=datetime.now(timezone.utc).isoformat(),
        model=model_id,
        turns_compacted=len(to_fold),
        reason=reason,
        prior_summary=prior_summary,
    )


def run_compaction_locked(
    workspace: Path,
    conversation_id: str,
    turns: Sequence[dict],
    model,
    *,
    reason: str,
    keep_recent: int = 0,
    model_id: str = "",
) -> Optional[CompactionRecord]:
    """Load prior, compact, and persist under a per-conversation lock."""
    with _lock_for(conversation_id):
        prior = load_compaction(workspace, conversation_id)
        record = compact_history(
            turns,
            model,
            reason=reason,
            prior=prior,
            keep_recent=keep_recent,
            model_id=model_id,
        )
        if record is None:
            return None
        save_compaction(workspace, conversation_id, record)
        logger.info(
            "Chat compaction for %s: reason=%s turns_compacted=%d watermark=%s",
            conversation_id,
            reason,
            record.turns_compacted,
            record.watermark_turn_id[:32],
        )
        return record


# Rough chars helper used by tests / callers that prefer character budgets.
def tokens_to_chars(tokens: int) -> int:
    return max(0, tokens) * CHARS_PER_TOKEN
