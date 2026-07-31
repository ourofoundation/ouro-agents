"""Replay a chat conversation to inspect history windowing.

Chat regressions like goal drift are only visible across a whole conversation:
one turn looks fine, and the damage shows up later when the history window has
moved past the evidence. This module replays a recorded transcript turn by turn
and reports, for each turn, which earlier turns would still be injected.

The agent's replies are replayed from the recording rather than regenerated, so
a replay is cheap and deterministic. Tests substitute an identity or window
function and pay nothing.

Transcripts come from the run log (every chat run stores its task and result)
or from a saved JSON fixture, so replays work offline against a past incident.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .chat_telemetry import estimate_turn_tokens

logger = logging.getLogger(__name__)

# Chat tasks arrive wrapped by the webhook builder; the transcript wants the
# user's actual words.
_WEBHOOK_PREFIX = re.compile(
    r"^New conversation message from .*?\(conversation_id: .*?\)\.\s*\n+",
    re.DOTALL,
)
_READY_HINT = re.compile(r"\n+\[?(?:Ready|Preloaded)[^\n]*\]?\s*$")

WindowFn = Callable[[list[dict]], list[dict]]


def strip_task_wrapper(task: str) -> str:
    """Recover the user's message from a wrapped chat task."""
    text = _WEBHOOK_PREFIX.sub("", task or "", count=1)
    return _READY_HINT.sub("", text).strip()


@dataclass
class ReplayTurn:
    """One user/assistant exchange in a replayed conversation."""

    index: int
    user: str
    assistant: str = ""
    run_id: Optional[str] = None
    started_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "user": self.user,
            "assistant": self.assistant,
            "run_id": self.run_id,
            "started_at": self.started_at,
        }


@dataclass
class ReplayStep:
    """The history window observed at one replayed turn."""

    turn: ReplayTurn
    turns_available: int = 0
    turns_injected: int = 0
    est_history_tokens: int = 0
    # Indices of ReplayTurns whose user content is still in the injected window.
    visible_turn_indices: list[int] = field(default_factory=list)

    @property
    def dropped_history_turns(self) -> int:
        return max(0, self.turns_available - self.turns_injected)


def load_transcript_from_run_log(
    store, conversation_id: str, *, limit: int = 200
) -> list[ReplayTurn]:
    """Rebuild a conversation transcript from recorded chat runs.

    Every chat run stores the task it received and the reply it produced, so
    the run log is a complete offline record of a conversation — no platform
    access needed to replay an incident.
    """
    rows = store.query_runs(
        mode="chat",
        conversation_id=conversation_id,
        limit=limit,
    )
    rows = sorted(rows, key=lambda r: r.get("started_at") or "")
    turns: list[ReplayTurn] = []
    for row in rows:
        user = strip_task_wrapper(str(row.get("task") or ""))
        if not user:
            continue
        turns.append(
            ReplayTurn(
                index=len(turns),
                user=user,
                assistant=str(row.get("result") or ""),
                run_id=row.get("run_id"),
                started_at=row.get("started_at"),
            )
        )
    return turns


def save_transcript(path: Path | str, turns: Sequence[ReplayTurn]) -> None:
    """Write a transcript to JSON so an incident can be replayed later."""
    payload = {"turns": [t.to_dict() for t in turns]}
    Path(path).write_text(json.dumps(payload, indent=2))


def load_transcript(path: Path | str) -> list[ReplayTurn]:
    """Read a transcript saved by :func:`save_transcript`."""
    data = json.loads(Path(path).read_text())
    raw = data.get("turns", data) if isinstance(data, dict) else data
    return [
        ReplayTurn(
            index=i,
            user=str(item.get("user", "")),
            assistant=str(item.get("assistant", "")),
            run_id=item.get("run_id"),
            started_at=item.get("started_at"),
        )
        for i, item in enumerate(raw)
    ]


def _identity_window(turns: list[dict]) -> list[dict]:
    return turns


def replay(
    turns: Sequence[ReplayTurn],
    *,
    window_fn: WindowFn = _identity_window,
) -> list[ReplayStep]:
    """Replay a transcript, tracking which earlier turns remain visible.

    ``window_fn`` is the history policy under test — pass the current
    :func:`~ouro_agents.utils.conversation.select_history_window` to reproduce
    today's cliff, or the default identity for append-only.
    """
    steps: list[ReplayStep] = []
    history: list[dict] = []
    # Map history-entry content back to the ReplayTurn that produced it.
    content_to_index: dict[str, int] = {}

    for turn in turns:
        injected = window_fn(list(history))
        visible: list[int] = []
        seen: set[int] = set()
        for entry in injected:
            if entry.get("role") != "user":
                continue
            content = str(entry.get("content", ""))
            idx = content_to_index.get(content)
            if idx is not None and idx not in seen:
                visible.append(idx)
                seen.add(idx)

        steps.append(
            ReplayStep(
                turn=turn,
                turns_available=len(history),
                turns_injected=len(injected),
                est_history_tokens=estimate_turn_tokens(injected),
                visible_turn_indices=visible,
            )
        )

        history.append({"role": "user", "content": turn.user})
        content_to_index[turn.user] = turn.index
        history.append({"role": "assistant", "content": turn.assistant})

    return steps


def turn_visible_at(
    steps: Sequence[ReplayStep], *, source_index: int, at_index: int
) -> bool:
    """True when the user turn at ``source_index`` is still injected at ``at_index``."""
    if at_index < 0 or at_index >= len(steps):
        return False
    return source_index in steps[at_index].visible_turn_indices


def first_turn_drop(
    steps: Sequence[ReplayStep], source_index: int
) -> Optional[ReplayStep]:
    """Return the first step where ``source_index`` left the injected window."""
    was_visible = False
    for step in steps:
        visible = source_index in step.visible_turn_indices
        if was_visible and not visible:
            return step
        if visible:
            was_visible = True
    return None


def format_trajectory(steps: Sequence[ReplayStep], *, width: int = 72) -> str:
    """Render a replay as a readable turn-by-turn report."""
    lines: list[str] = []
    for step in steps:
        turn = step.turn
        lines.append(f"--- turn {turn.index} " + "-" * max(0, width - 12))
        lines.append(f"user: {_clip(turn.user, width)}")
        lines.append(f"assistant: {_clip(turn.assistant, width)}")
        lines.append(
            f"  history: {step.turns_injected}/{step.turns_available} turns"
            f" (~{step.est_history_tokens} tok)"
            + (
                f"  DROPPED {step.dropped_history_turns}"
                if step.dropped_history_turns
                else ""
            )
        )
        if step.visible_turn_indices:
            lines.append(
                f"  visible user turns: {step.visible_turn_indices}"
            )
    return "\n".join(lines)


def _clip(text: str, width: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
