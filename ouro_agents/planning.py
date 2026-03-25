"""Planning cycle: plan / review / execute layered on top of heartbeats.

The agent periodically generates a plan (published as an Ouro post for human
review), then executes guided by it.  Configurable at any timescale with a
min-heartbeats guard so the agent doesn't spend all its time planning.

Cycle states:
    planning  →  pending_review  →  active  →  (back to planning)
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class PlanCycle(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["planning", "pending_review", "active", "completed"] = "planning"
    plan_text: str = ""
    post_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    activated_at: Optional[str] = None
    completed_at: Optional[str] = None
    heartbeats_completed: int = 0
    human_feedback: Optional[str] = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class PlanStore:
    """Read/write plan cycles as JSON files in the workspace."""

    def __init__(self, plans_dir: Path):
        self._dir = plans_dir
        self._current_path = plans_dir / "current.json"
        self._history_dir = plans_dir / "history"

    def load_current(self) -> Optional[PlanCycle]:
        if not self._current_path.exists():
            return None
        try:
            data = json.loads(self._current_path.read_text())
            return PlanCycle(**data)
        except Exception:
            logger.exception("Failed to load current plan cycle from %s", self._current_path)
            return None

    def save_current(self, cycle: PlanCycle) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        data = cycle.model_dump()
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._current_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def archive_current(self) -> Optional[PlanCycle]:
        """Move the current cycle to history and clear current."""
        current = self.load_current()
        if not current:
            return None

        current.status = "completed"
        current.completed_at = datetime.now(timezone.utc).isoformat()

        self._history_dir.mkdir(parents=True, exist_ok=True)
        history_path = self._history_dir / f"{current.id}.json"
        history_path.write_text(json.dumps(current.model_dump(), indent=2))

        self._current_path.unlink(missing_ok=True)
        return current

    def load_history(self, limit: int = 5) -> list[PlanCycle]:
        if not self._history_dir.exists():
            return []
        files = sorted(self._history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        cycles: list[PlanCycle] = []
        for f in files[:limit]:
            try:
                cycles.append(PlanCycle(**json.loads(f.read_text())))
            except Exception:
                logger.warning("Skipping corrupt plan history file: %s", f)
        return cycles


# ---------------------------------------------------------------------------
# Interval parsing (reuses the scheduler's format)
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")


def parse_cadence_seconds(cadence: str) -> Optional[int]:
    """Convert an interval shorthand like '4h' or '1d' to seconds.

    Returns None for cron expressions (which need different handling).
    """
    m = _INTERVAL_RE.match(cadence.strip())
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multiplier[unit]


# ---------------------------------------------------------------------------
# Cycle decision logic
# ---------------------------------------------------------------------------

def next_action(
    current: Optional[PlanCycle],
    cadence: str,
    min_heartbeats: int,
    review_window: str,
    auto_approve: bool,
    now: Optional[datetime] = None,
) -> Literal["plan", "check_review", "execute"]:
    """Determine what the heartbeat should do given the current plan cycle state."""
    now = now or datetime.now(timezone.utc)

    if current is None:
        return "plan"

    if current.status == "planning":
        return "plan"

    if current.status == "pending_review":
        if current.human_feedback:
            return "execute"

        review_secs = parse_cadence_seconds(review_window)
        if review_secs:
            created = datetime.fromisoformat(current.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            elapsed = (now - created).total_seconds()
            if elapsed >= review_secs and auto_approve:
                return "execute"

        return "check_review"

    if current.status == "active":
        cadence_secs = parse_cadence_seconds(cadence)
        if cadence_secs and current.heartbeats_completed >= min_heartbeats:
            activated = datetime.fromisoformat(current.activated_at or current.created_at)
            if activated.tzinfo is None:
                activated = activated.replace(tzinfo=timezone.utc)
            elapsed = (now - activated).total_seconds()
            if elapsed >= cadence_secs:
                return "plan"
        return "execute"

    # completed or unknown — start fresh
    return "plan"


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PLANNING_PROMPT_TEMPLATE = """\
You are entering a planning phase. Review what has happened since your last plan
(or recently if this is your first), consider your scheduled tasks, ongoing work,
and interests, then create a plan for the upcoming period.

Your plan should be:
- Realistic given the time available (~{cadence_description})
- Specific enough to guide your heartbeats
- Flexible enough to adapt if priorities shift

{previous_plan_section}

{context_section}

When you are done:
1. Create an Ouro post with your plan{post_instructions}.
   Title it something like "Plan: {time_label}".
   Write the plan in clear markdown with actionable items.
2. Return a JSON summary:
```json
{{"plan": "<your full plan text (markdown)>", "post_id": "<asset id returned by create_post>"}}
```
"""

REVIEW_PROMPT_TEMPLATE = """\
You published a plan as an Ouro post (asset ID: {post_id}).
Check if there are any comments on that post with feedback from a human reviewer.

Your current plan:
{plan_text}

If there is feedback, revise your plan to incorporate it. If there are no comments
or the comments don't require changes, keep the plan as-is.

Return a JSON summary:
```json
{{"revised_plan": "<the updated plan text, or the original if no changes>", "feedback_summary": "<brief summary of feedback received, or null if none>"}}
```
"""


def _cadence_description(cadence: str) -> str:
    """Human-readable description of the cadence interval."""
    m = _INTERVAL_RE.match(cadence.strip())
    if not m:
        return cadence
    val = int(m.group(1))
    labels = {"s": "second", "m": "minute", "h": "hour", "d": "day"}
    label = labels[m.group(2)]
    if val != 1:
        label += "s"
    return f"{val} {label}"


def build_planning_prompt(
    cadence: str,
    team_id: Optional[str] = None,
    org_id: Optional[str] = None,
    previous_plan: Optional[PlanCycle] = None,
    extra_context: str = "",
) -> str:
    post_parts = []
    if org_id:
        post_parts.append(f"org_id=\"{org_id}\"")
    if team_id:
        post_parts.append(f"team_id=\"{team_id}\"")
    post_instructions = f" (use {', '.join(post_parts)})" if post_parts else ""

    previous_plan_section = ""
    if previous_plan and previous_plan.plan_text:
        previous_plan_section = (
            "## Previous Plan\n"
            "Here is your most recent completed plan for reference:\n"
            f"{previous_plan.plan_text}\n"
        )

    context_section = ""
    if extra_context:
        context_section = f"## Additional Context\n{extra_context}"

    now = datetime.now().astimezone()
    time_label = now.strftime("%B %d, %Y %H:%M")

    return PLANNING_PROMPT_TEMPLATE.format(
        cadence_description=_cadence_description(cadence),
        post_instructions=post_instructions,
        previous_plan_section=previous_plan_section,
        context_section=context_section,
        time_label=time_label,
    )


FEEDBACK_REVIEW_PROMPT_TEMPLATE = """\
You received direct feedback on your plan (Ouro post ID: {post_id}).

Your current plan:
{plan_text}

Feedback received:
{feedback_text}

Revise your plan to incorporate this feedback. If the feedback doesn't require
changes, keep the plan as-is. Reply to the commenter acknowledging their input
(create_comment on the post). If you revise the plan, update the post too
(update_post).

Return a JSON summary:
```json
{{"revised_plan": "<the updated plan text, or the original if no changes>", "feedback_summary": "<brief summary of feedback received>"}}
```
"""


def build_review_prompt(post_id: str, plan_text: str) -> str:
    return REVIEW_PROMPT_TEMPLATE.format(post_id=post_id, plan_text=plan_text)


def build_feedback_review_prompt(
    post_id: str, plan_text: str, feedback_text: str
) -> str:
    return FEEDBACK_REVIEW_PROMPT_TEMPLATE.format(
        post_id=post_id, plan_text=plan_text, feedback_text=feedback_text
    )
