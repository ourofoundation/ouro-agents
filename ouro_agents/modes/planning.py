"""Planning cycle: plan / review / execute layered on top of heartbeats.

The agent periodically generates a plan (published as an Ouro post for human
review), then executes guided by it.  Configurable at any timescale with a
min-heartbeats guard so the agent doesn't spend all its time planning.

Multiple plans can be active simultaneously:
  - **default** plan: driven by the cadence config, auto-replans on schedule.
  - **goal** plans: user-initiated via a specific prompt/goal, coexist with
    the default plan and complete when all items are done.

Cycle states:
    planning  →  pending_review  →  active  →  (back to planning | completed)
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ..agent import OuroAgent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class PlanItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    description: str
    status: Literal["pending", "in_progress", "done", "skipped"] = "pending"
    notes: str = ""


class PlanCycle(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["planning", "pending_review", "active", "completed", "cancelled"] = "planning"
    kind: Literal["default", "goal"] = "default"
    goal: str = ""
    plan_text: str = ""
    items: list[PlanItem] = []
    post_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    activated_at: Optional[str] = None
    completed_at: Optional[str] = None
    heartbeats_completed: int = 0
    human_feedback: Optional[str] = None
    revision_count: int = 0

    @property
    def items_done(self) -> int:
        return sum(1 for i in self.items if i.status in ("done", "skipped"))

    @property
    def all_items_complete(self) -> bool:
        return bool(self.items) and all(
            i.status in ("done", "skipped") for i in self.items
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class PlanStore:
    """Read/write plan cycles as JSON files in the workspace.

    Supports multiple concurrent active plans stored under ``active/``:
    the default cadence plan lives at ``active/default.json`` while
    user-created goal plans live at ``active/goal-{id}.json``.  Completed
    plans are archived under ``history/``.

    Legacy ``current.json`` files are auto-migrated on first access.
    """

    def __init__(self, plans_dir: Path):
        self._dir = plans_dir
        self._active_dir = plans_dir / "active"
        self._history_dir = plans_dir / "history"
        self._migrate_legacy()

    # -- migration from single-file current.json --------------------------

    def _migrate_legacy(self) -> None:
        legacy = self._dir / "current.json"
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text())
            cycle = PlanCycle(**data)
            if cycle.kind not in ("default", "goal"):
                cycle.kind = "default"
            self.save(cycle)
            legacy.unlink(missing_ok=True)
            logger.info("Migrated legacy current.json → active/%s", self._filename(cycle))
        except Exception:
            logger.exception("Failed to migrate legacy current.json")

    # -- path helpers ------------------------------------------------------

    def _filename(self, cycle: PlanCycle) -> str:
        if cycle.kind == "default":
            return "default.json"
        return f"goal-{cycle.id}.json"

    def _plan_path(self, cycle: PlanCycle) -> Path:
        return self._active_dir / self._filename(cycle)

    # -- read operations ---------------------------------------------------

    def _load_file(self, path: Path) -> Optional[PlanCycle]:
        if not path.exists():
            return None
        try:
            return PlanCycle(**json.loads(path.read_text()))
        except Exception:
            logger.warning("Failed to load plan from %s", path)
            return None

    def load_default(self) -> Optional[PlanCycle]:
        """Load the default cadence plan (if any)."""
        return self._load_file(self._active_dir / "default.json")

    def load_all_active(self) -> list[PlanCycle]:
        """Load every active plan (default + goals)."""
        if not self._active_dir.exists():
            return []
        plans: list[PlanCycle] = []
        for f in sorted(self._active_dir.glob("*.json")):
            cycle = self._load_file(f)
            if cycle:
                plans.append(cycle)
        return plans

    def load_by_post_id(self, post_id: str) -> Optional[PlanCycle]:
        """Find an active plan by its Ouro post ID."""
        for cycle in self.load_all_active():
            if cycle.post_id == post_id:
                return cycle
        return None

    def load_by_id(self, cycle_id: str) -> Optional[PlanCycle]:
        """Find an active plan by its cycle ID (prefix match)."""
        for cycle in self.load_all_active():
            if cycle.id.startswith(cycle_id):
                return cycle
        return None

    # -- write operations --------------------------------------------------

    def save(self, cycle: PlanCycle) -> None:
        """Persist a plan cycle to the active directory."""
        self._active_dir.mkdir(parents=True, exist_ok=True)
        dest = self._plan_path(cycle)
        data = cycle.model_dump()
        fd, tmp = tempfile.mkstemp(dir=self._active_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, dest)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def archive(self, cycle: PlanCycle, ouro_client=None) -> PlanCycle:
        """Move a plan to history and remove it from active.

        If *ouro_client* is provided and the cycle has a post_id, the Ouro
        post description is updated to reflect its final status.
        """
        if cycle.status not in ("completed", "cancelled"):
            cycle.status = "completed"
        cycle.completed_at = datetime.now(timezone.utc).isoformat()

        if ouro_client and cycle.post_id:
            update_post_status(ouro_client, cycle)

        self._history_dir.mkdir(parents=True, exist_ok=True)
        history_path = self._history_dir / f"{cycle.id}.json"
        history_path.write_text(json.dumps(cycle.model_dump(), indent=2))

        active_path = self._plan_path(cycle)
        active_path.unlink(missing_ok=True)
        return cycle

    # -- backward-compat aliases -------------------------------------------

    def load_current(self) -> Optional[PlanCycle]:
        """Alias for :meth:`load_default` (backward compatibility)."""
        return self.load_default()

    def save_current(self, cycle: PlanCycle) -> None:
        """Alias for :meth:`save` (backward compatibility)."""
        self.save(cycle)

    def archive_current(self, ouro_client=None) -> Optional[PlanCycle]:
        """Archive the default plan (backward compatibility)."""
        default = self.load_default()
        if not default:
            return None
        return self.archive(default, ouro_client=ouro_client)

    # -- history -----------------------------------------------------------

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
# Plan item helpers
# ---------------------------------------------------------------------------

_CHECKBOX_RE = re.compile(r"^\[( |x)\]\s*(?:\((\w+)\)\s*)?(.+)$", re.MULTILINE)


def render_plan_markdown(items: list[PlanItem]) -> str:
    """Render plan items as checkbox markdown ([] / [x] format)."""
    lines: list[str] = []
    for item in items:
        check = "x" if item.status in ("done", "skipped") else " "
        line = f"[{check}] ({item.id}) {item.description}"
        if item.notes:
            line += f" — {item.notes}"
        lines.append(line)
    return "\n".join(lines)


_CHECKBOX_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-*+]\s+)?)"   # leading whitespace + optional bullet
    r"\[(?P<check>[ xX])\]\s*"         # checkbox
    r"(?:\([0-9a-f]+\)\s*)?"           # optional (id) from a previous sync
    r"(?P<desc>.+?)(?:\s*—\s*.+)?$",   # description (strip trailing notes)
)


def _normalize(text: str) -> str:
    """Lowercase, strip markdown bold/italic and extra whitespace."""
    t = text.strip().lower()
    t = re.sub(r"\*+", "", t)
    return " ".join(t.split())


_MD_TASK_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-*+]\s+)?)"
    r"\[(?P<check>[ xX])\]\s*"
    r"(?:\((?P<item_id>[0-9a-f]{8})\)\s*)?"
    r"(?P<desc>.+?)(?:\s*—\s*(?P<notes>.+))?\s*$"
)


def parse_task_lines_from_markdown(markdown: str) -> list[tuple[str, bool, str]]:
    """Extract GFM task lines as (description, is_checked_done, notes).

    *is_checked_done* is True when the checkbox is ``[x]`` or ``[X]``.
    Trailing `` — notes`` after the description is captured when present.
    """
    rows: list[tuple[str, bool, str]] = []
    if not markdown:
        return rows
    for line in markdown.split("\n"):
        m = _MD_TASK_LINE_RE.match(line.rstrip())
        if not m:
            continue
        desc = (m.group("desc") or "").strip()
        if not desc:
            continue
        done = (m.group("check") or "").lower() == "x"
        notes = (m.group("notes") or "").strip()
        rows.append((desc, done, notes))
    return rows


def sync_plan_items_from_markdown(
    markdown: str, existing: list[PlanItem],
) -> list[PlanItem]:
    """Rebuild plan items from task lines in *markdown*, reusing ids when descriptions match.

    Tasks that appear in *markdown* but not in *existing* get new ids. Tasks that
    exist only in *existing* are dropped. Preserves ``in_progress`` / ``skipped``
    when the markdown checkbox is unchecked; ``[x]`` forces ``done``.
    """
    rows = parse_task_lines_from_markdown(markdown)
    pool: dict[str, list[PlanItem]] = defaultdict(list)
    for it in existing:
        pool[_normalize(it.description)].append(it)

    out: list[PlanItem] = []
    for desc, md_done, line_notes in rows:
        key = _normalize(desc)
        old = pool[key].pop(0) if pool[key] else None
        if old:
            if md_done:
                status: Literal["pending", "in_progress", "done", "skipped"] = "done"
            else:
                status = old.status
            note = line_notes if line_notes else old.notes
            out.append(PlanItem(id=old.id, description=desc, status=status, notes=note))
        else:
            out.append(PlanItem(
                description=desc,
                status="done" if md_done else "pending",
                notes=line_notes,
            ))
    return out


def rebuild_plan_markdown(plan_text: str, items: list[PlanItem]) -> str:
    """Update checkbox statuses inside *plan_text* while preserving all prose.

    Matches checklist lines in the original plan_text to PlanItems by
    normalised description.  Outputs valid GFM task-list syntax so the
    backend markdown parser produces proper tiptap checklist nodes.
    Unmatched items are appended at the end in GFM format.
    """
    if not plan_text:
        return render_plan_markdown(items)
    if not items:
        return plan_text

    items_by_desc: dict[str, PlanItem] = {}
    for item in items:
        items_by_desc[_normalize(item.description)] = item

    lines = plan_text.split("\n")
    result: list[str] = []
    matched_ids: set[str] = set()

    for line in lines:
        m = _CHECKBOX_LINE_RE.match(line)
        if m:
            prefix = m.group("prefix")
            raw_desc = m.group("desc")
            norm = _normalize(raw_desc)
            item = items_by_desc.get(norm)
            if item:
                check = "x" if item.status in ("done", "skipped") else " "
                if not re.match(r"\s*[-*+]\s+", prefix):
                    prefix = prefix + "- "
                new_line = f"{prefix}[{check}] {item.description}"
                if item.notes:
                    new_line += f" — {item.notes}"
                result.append(new_line)
                matched_ids.add(item.id)
                continue
        result.append(line)

    unmatched = [i for i in items if i.id not in matched_ids]
    if unmatched:
        result.append("")
        for item in unmatched:
            check = "x" if item.status in ("done", "skipped") else " "
            line = f"- [{check}] {item.description}"
            if item.notes:
                line += f" — {item.notes}"
            result.append(line)

    return "\n".join(result)


def render_plan_context(cycle: PlanCycle) -> str:
    """Build the structured plan context block injected into the heartbeat playbook."""
    total = len(cycle.items)
    done = cycle.items_done
    if cycle.kind == "goal" and cycle.goal:
        label = f"Goal Plan: {cycle.goal}"
    else:
        label = "Default Plan"
    parts = [f"## {label} (id: {cycle.id[:8]}, post: {cycle.post_id or 'n/a'})"]
    if total:
        parts.append(f"Progress: {done}/{total} items complete\n")
        parts.append(render_plan_markdown(cycle.items))
    elif cycle.plan_text:
        parts.append(cycle.plan_text)
    return "\n".join(parts)


def format_plans_index_for_prompt(plans: list[PlanCycle]) -> str:
    """Short list of plan Ouro post ids for system prompts (no plan body).

    Lets the model call ``get_asset`` on a post id when it needs the full plan.
    """

    lines: list[str] = []
    for p in plans:
        if p.status not in ("active", "pending_review") or not p.post_id:
            continue
        lines.append(
            f"- `{p.post_id}` — asset type: post; plan kind: {p.kind}; "
            f"status: {p.status}; cycle_id: {p.id[:8]}"
        )
    if not lines:
        return ""
    return (
        "These posts hold plan content on the platform. "
        "Use `get_asset` with the post id if you need the full text.\n\n"
        + "\n".join(lines)
    )


def render_all_plans_context(plans: list[PlanCycle]) -> str:
    """Build context for all active plans, injected into the heartbeat playbook."""
    if not plans:
        return ""
    parts = [render_plan_context(p) for p in plans if p.status == "active"]
    if not parts:
        return ""
    return "\n\n".join(parts)


def parse_plan_items(raw: list[dict]) -> list[PlanItem]:
    """Parse the LLM's JSON items array into PlanItem instances."""
    items: list[PlanItem] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"description": entry}
        items.append(PlanItem(
            description=entry.get("description", ""),
            status=entry.get("status", "pending"),
            notes=entry.get("notes", ""),
        ))
    return items


def plan_post_description(cycle: PlanCycle, auto_activate_at: Optional[str] = None) -> str:
    """Compute the description/subtitle for a plan post based on its current state."""
    total = len(cycle.items)
    done = cycle.items_done
    progress = f"{done}/{total} items complete" if total else ""
    prefix = f"Goal: {cycle.goal[:80]} — " if cycle.kind == "goal" and cycle.goal else ""

    if cycle.status == "cancelled":
        return f"{prefix}[cancelled]"

    if cycle.status == "pending_review":
        if cycle.revision_count > 0:
            suffix = f" (revision {cycle.revision_count})"
            if auto_activate_at:
                return f"{prefix}Revised — pending approval, will auto-activate at {auto_activate_at}{suffix}"
            return f"{prefix}Revised — pending approval{suffix}"
        if auto_activate_at:
            return f"{prefix}Pending review — will auto-activate at {auto_activate_at}"
        return f"{prefix}Pending review"

    if cycle.status == "active":
        if cycle.all_items_complete:
            return f"{prefix}Completed — {progress}"
        base = f"Active — {progress}" if progress else "Active"
        return f"{prefix}{base}"

    if cycle.status == "completed":
        completed_progress = f"{done}/{total} items completed" if total else "completed"
        return f"{prefix}[archived] {completed_progress}"

    return ""


def _sync_post(
    ouro_client, post_id: str, items: list[PlanItem], plan_text: str,
    description: Optional[str] = None,
) -> None:
    """Update the Ouro post content and optionally its description.

    Uses *plan_text* as the base document and patches checkbox statuses from
    *items*, so headings, prose, and other non-checklist content are preserved.
    """
    if not ouro_client or not post_id:
        return
    try:
        markdown = rebuild_plan_markdown(plan_text, items)
        content = ouro_client.posts.Content()
        content.from_markdown(markdown)
        kwargs = {"content": content}
        if description is not None:
            kwargs["description"] = description
        ouro_client.posts.update(post_id, **kwargs)
    except Exception as e:
        logger.warning("Failed to sync plan post %s: %s", post_id, e)


def update_post_status(ouro_client, cycle: PlanCycle, **kwargs) -> None:
    """Update just the description/metadata on the plan post (no content change)."""
    if not ouro_client or not cycle.post_id:
        return
    try:
        desc = plan_post_description(cycle, **kwargs)
        if desc:
            ouro_client.posts.update(cycle.post_id, description=desc)
    except Exception as e:
        logger.warning("Failed to update plan post status %s: %s", cycle.post_id, e)


def comment_on_plan(ouro_client, post_id: str, markdown: str) -> None:
    """Post a comment on the plan post to communicate status changes."""
    if not ouro_client or not post_id:
        return
    try:
        content = ouro_client.posts.Content()
        content.from_markdown(markdown)
        ouro_client.comments.create(content=content, parent_id=post_id)
    except Exception as e:
        logger.warning("Failed to comment on plan post %s: %s", post_id, e)


# ---------------------------------------------------------------------------
# Agent-facing tool
# ---------------------------------------------------------------------------

def make_plan_tools(plan_store: "PlanStore", ouro_client=None) -> list:
    """Create the update_plan tool bound to a plan store and optional Ouro client."""
    from smolagents import tool

    @tool
    def update_plan(updates: list, plan_id: str = "") -> str:
        """Update plan item statuses. Call this when you complete, start, or skip a plan item.

        Args:
            updates: List of updates. Each is a dict with keys:
                - id (str, required): The item ID shown in parentheses in the plan, e.g. "a1b2c3d4"
                - status (str, required): One of "done", "in_progress", "pending", "skipped"
                - notes (str, optional): Brief note about what was done or why skipped
            plan_id: The 8-char plan ID to update (shown in plan context headers).
                     If omitted, updates the default plan.
        """
        if plan_id:
            cycle = plan_store.load_by_id(plan_id)
        else:
            cycle = plan_store.load_default()

        if not cycle or not cycle.items:
            return json.dumps({"error": "No active plan with items."})

        items_by_id = {item.id: item for item in cycle.items}
        applied = []
        errors = []

        was_complete = cycle.all_items_complete

        for update in updates:
            if isinstance(update, str):
                errors.append(f"Invalid update (expected dict): {update}")
                continue
            item_id = update.get("id", "")
            item = items_by_id.get(item_id)
            if not item:
                errors.append(f"Item '{item_id}' not found")
                continue
            new_status = update.get("status", "")
            if new_status not in ("done", "in_progress", "pending", "skipped"):
                errors.append(f"Invalid status '{new_status}' for item '{item_id}'")
                continue
            item.status = new_status
            if "notes" in update:
                item.notes = update["notes"]
            applied.append(f"{item_id}: {new_status}")

        plan_store.save(cycle)
        desc = plan_post_description(cycle)
        _sync_post(ouro_client, cycle.post_id, cycle.items, cycle.plan_text, description=desc)

        done = cycle.items_done
        total = len(cycle.items)
        result = {"progress": f"{done}/{total} complete", "updated": applied}
        if errors:
            result["errors"] = errors
        return json.dumps(result)

    return [update_plan]


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
        # Replan when all items are complete
        if current.all_items_complete and current.heartbeats_completed >= min_heartbeats:
            return "plan"

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
You are entering a planning phase.{goal_section}

Review what has happened since your last plan (or recently if this is your first),
consider your scheduled tasks, ongoing work, and interests, then create a plan
for the upcoming period.

Your plan should be realistic given the time available (~{cadence_description}),
specific enough to guide your heartbeats, and flexible enough to adapt.

{previous_plan_section}

{context_section}

IMPORTANT: You MUST do both of the following steps. Do NOT skip the post creation.
Do NOT use any tools besides create_post and final_answer. Do NOT attempt to
execute any plan items or do actual work — only write and publish the plan.

Step 1. Call create_post to publish your plan{post_instructions}.
   Name it exactly "{post_name}".

   FORMAT RULES — follow these strictly:
   - Use **headers** (##, ###) to organize sections.
   - Write **prose paragraphs** for context, reasoning, and descriptions.
     Do NOT use bullet lists for context or notes — write sentences and paragraphs.
   - The ONLY lists in the document should be GFM task items for concrete,
     actionable tasks you will complete and check off:
       - [ ] Task to do
       - [x] Completed task
   - Avoid bullet lists, numbered lists, or nested sub-items for anything
     that is not an actionable task. Use paragraphs instead.

   Good example:
     ## Context
     Recent work focused on X. The Y series is complete and well-received.
     ## Focus Areas
     ### Topic A
     This period will focus on researching A because of Z.
     ### Topic B
     Secondary priority is B, building on previous work.
     ## Tasks
     - [ ] Write research guide on Topic A
     - [ ] Review comments on recent posts
     - [ ] Monitor team feed for updates

   Bad example (too listy):
     ## Context
     - **Recent work**: X, Y, Z
     - **Time available**: 4 hours
     ## Focus Areas
     - **Topic A**: research A
       - Sub-point 1
       - Sub-point 2
     - **Topic B**: do B

Step 2. After create_post succeeds, call final_answer with this JSON:
```json
{{"plan": "<your full plan text (markdown)>", "post_id": "<the asset id from create_post>", "items": [{{"description": "item text"}}, ...]}}
```
"""

CONTINUATION_PLANNING_PROMPT_TEMPLATE = """\
You are revising your current plan.{goal_section}

Review what has happened, what's been completed, and what needs to change
for the upcoming period (~{cadence_description}).

## Current Plan
{current_items_section}

{previous_plan_section}

{context_section}

You may add new items, remove items that are no longer relevant, or adjust priorities.
Keep items that are already done marked as done.

IMPORTANT: You MUST do both of the following steps. Do NOT skip the post update.
Do NOT use any tools besides update_post and final_answer. Do NOT attempt to
execute any plan items or do actual work — only revise and update the plan.

Step 1. Call update_post to revise the existing plan post (id: {post_id}){post_instructions}.

   FORMAT RULES — follow these strictly:
   - Use **headers** (##, ###) to organize sections.
   - Write **prose paragraphs** for context, reasoning, and descriptions.
     Do NOT use bullet lists for context or notes — write sentences and paragraphs.
   - The ONLY lists in the document should be GFM task items for concrete,
     actionable tasks you will complete and check off:
       - [ ] Task to do
       - [x] Completed task
   - Avoid bullet lists, numbered lists, or nested sub-items for anything
     that is not an actionable task. Use paragraphs instead.

Step 2. After update_post succeeds, call final_answer with this JSON:
```json
{{"plan": "<your revised plan text (markdown)>", "items": [{{"description": "item text", "status": "done|pending"}}, ...]}}
```
"""

REVIEW_PROMPT_TEMPLATE = """\
You published a plan as an Ouro post (asset ID: {post_id}).
Check if there are any comments on that post with feedback from a human reviewer
(use get_comments).

Current plan status: {current_status}

Your current plan:
{plan_text}

If there is feedback, revise your plan to incorporate it and update the post
(update_post).  Then reply to the reviewer with a comment (create_comment on the
post) summarizing what you changed and what the plan's next status should be.

If there are no comments or the comments don't require changes, keep the plan as-is.

Do NOT use any tools besides get_comments, create_comment, update_post, and
final_answer. Do NOT execute any plan items — only review feedback and revise.

IMPORTANT — approval vs. revision:
- Set "next_status": "active" ONLY when the reviewer explicitly approves the plan
  with positive affirmation (e.g. "good to go", "approved", "looks good", "ship it").
- Set "next_status": "pending_review" when the reviewer requests changes or provides
  directional feedback WITHOUT explicit approval. The plan will be revised and
  re-posted for another round of review.
- If the reviewer asks you to deactivate, cancel, stop, shelve, or archive the
  plan, set "next_status": "cancelled" and keep "revised_plan" equal to the
  current plan text unless explicit content edits were requested.
- If there are no comments at all, set feedback_summary to null and
  "next_status": "pending_review" if the plan is still awaiting review, or
  "active" if it is already active.

Return a JSON summary:
```json
{{"revised_plan": "<the updated plan text, or the original if no changes>", "feedback_summary": "<brief summary of feedback received, or null if none>", "next_status": "active|pending_review|cancelled"}}
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
    current_plan: Optional[PlanCycle] = None,
    extra_context: str = "",
    agent_name: str = "",
    goal: str = "",
) -> str:
    """Build a planning or continuation-planning prompt.

    If *current_plan* has a post_id, produces a continuation prompt that asks
    the LLM to update the existing post.  Otherwise produces a fresh-plan
    prompt that asks the LLM to create a new post.

    When *goal* is provided the plan is framed around achieving that goal.
    """
    post_parts = []
    if org_id:
        post_parts.append(f"org_id=\"{org_id}\"")
    if team_id:
        post_parts.append(f"team_id=\"{team_id}\"")
    post_instructions = f" (use {', '.join(post_parts)})" if post_parts else ""

    goal_section = ""
    if goal:
        goal_section = (
            f" The focus area for this planning period is:\n\n"
            f"> {goal}\n\n"
            f"Structure your plan around this focus area."
        )

    # Continuation planning: update existing post
    if current_plan and current_plan.post_id:
        current_items_section = render_plan_markdown(current_plan.items) if current_plan.items else current_plan.plan_text

        previous_plan_section = ""
        if previous_plan and previous_plan.plan_text:
            previous_plan_section = (
                "## Previous Completed Plan\n"
                f"{previous_plan.plan_text}\n"
            )

        context_section = ""
        if extra_context:
            context_section = f"## Additional Context\n{extra_context}"

        return CONTINUATION_PLANNING_PROMPT_TEMPLATE.format(
            cadence_description=_cadence_description(cadence),
            current_items_section=current_items_section,
            post_id=current_plan.post_id,
            post_instructions=post_instructions,
            previous_plan_section=previous_plan_section,
            context_section=context_section,
            goal_section=goal_section,
        )

    # Fresh plan: create a new post
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
    date_label = now.strftime("%Y-%m-%d")
    if goal:
        goal_slug = goal[:40].strip().rstrip(".")
        post_name = f"PLAN:{agent_name}:goal:{goal_slug}" if agent_name else f"PLAN:goal:{goal_slug}"
    else:
        post_name = f"PLAN:{agent_name}:{date_label}" if agent_name else f"PLAN:{date_label}"

    return PLANNING_PROMPT_TEMPLATE.format(
        cadence_description=_cadence_description(cadence),
        post_instructions=post_instructions,
        previous_plan_section=previous_plan_section,
        context_section=context_section,
        post_name=post_name,
        goal_section=goal_section,
    )


FEEDBACK_REVIEW_PROMPT_TEMPLATE = """\
You received direct feedback on your plan (Ouro post ID: {post_id}).

Current plan status: {current_status}

Your current plan:
{plan_text}

Feedback received:
{feedback_text}

{thread_context}

Steps:
1. Revise your plan to incorporate this feedback (if changes are needed).
2. If you revised the plan, update the post (update_post).
3. {reply_instruction} Summarize what you changed, and if the plan is not yet
   approved, note that you're awaiting their go-ahead before executing.

Do NOT use any tools besides get_comments, create_comment, update_post, and final_answer.
Do NOT execute any plan items — only review feedback and revise.

IMPORTANT — approval vs. revision:
- {approval_guidance}

IMPORTANT — deactivation / cancellation:
- If the reviewer asks you to deactivate, cancel, stop, shelve, or archive the
  plan, do NOT rewrite the plan body unless they also asked for textual edits.
- In that case, set "next_status": "cancelled" and keep "revised_plan" equal
  to the current plan text unless explicit content edits were requested.
- A cancelled plan should be treated as inactive and should no longer be
  awaiting approval or execution.

Return a JSON summary:
```json
{{"revised_plan": "<the updated plan text, or the original if no changes>", "feedback_summary": "<brief summary of feedback received>", "next_status": "active|pending_review|cancelled"}}
```
"""


def build_review_prompt(post_id: str, plan_text: str, current_status: str) -> str:
    return REVIEW_PROMPT_TEMPLATE.format(
        post_id=post_id,
        plan_text=plan_text,
        current_status=current_status,
    )


def build_feedback_review_prompt(
    post_id: str,
    plan_text: str,
    feedback_text: str,
    current_status: str,
    reply_parent_id: str | None = None,
    thread_parent_id: str | None = None,
) -> str:
    if reply_parent_id and reply_parent_id != post_id:
        reply_instruction = (
            f"Reply in the same comment thread by calling create_comment with "
            f"parent_id `{reply_parent_id}`."
        )
    else:
        reply_instruction = (
            f"Reply on the plan post by calling create_comment with parent_id `{post_id}`."
        )

    if thread_parent_id:
        thread_context = (
            f"If you need more thread context, inspect the active discussion thread with "
            f"get_comments(parent_id=`{thread_parent_id}`). The relevant thread may also "
            f"already be included in your prefetched context."
        )
    else:
        thread_context = ""

    if current_status == "active":
        approval_guidance = (
            "This plan is already active, which means it has already been approved. "
            'Keep it active after incorporating feedback unless the reviewer explicitly '
            'asks you to pause, stop, or hold execution pending another review. In the '
            'normal case, set "next_status": "active" and reply as someone continuing an '
            'active plan, not as someone waiting for initial approval.'
        )
    else:
        approval_guidance = (
            'Set "next_status": "active" ONLY when the feedback explicitly approves the plan '
            'with positive affirmation (e.g. "good to go", "approved", "looks good", "ship it"). '
            'Set "next_status": "pending_review" when the reviewer requests changes or gives '
            "direction WITHOUT explicit approval. The plan will be revised and re-posted for "
            "another round of review."
        )

    return FEEDBACK_REVIEW_PROMPT_TEMPLATE.format(
        post_id=post_id,
        current_status=current_status,
        plan_text=plan_text,
        feedback_text=feedback_text,
        reply_instruction=reply_instruction,
        thread_context=thread_context,
        approval_guidance=approval_guidance,
    )


# ---------------------------------------------------------------------------
# Orchestration (previously in agent.py)
# ---------------------------------------------------------------------------


async def run_planning_heartbeat(
    agent: OuroAgent,
    hb_model,
    plan_store: PlanStore,
    servers: list[str],
    continuation: Optional[PlanCycle] = None,
    goal: str = "",
    kind: str = "default",
) -> Optional[str]:
    """Run a planning heartbeat: generate or revise a plan.

    If *continuation* is provided (an active cycle with a post_id), the
    prompt asks the LLM to revise and update the existing post.  Otherwise
    a fresh plan is created.

    When *goal* is given the plan is framed around achieving it.
    *kind* controls whether this is a ``"default"`` cadence plan or a
    ``"goal"`` plan that coexists alongside the default.
    """
    from ..memory.reflection import write_daily_log
    from .profiles import RunMode

    planning_cfg = agent.config.planning
    previous = plan_store.load_history(limit=1)
    previous_plan = previous[0] if previous else None

    is_continuation = continuation and continuation.post_id
    mem_cfg = agent.config.memory
    prompt = build_planning_prompt(
        cadence=planning_cfg.cadence,
        team_id=mem_cfg.team_id or planning_cfg.team_id,
        org_id=mem_cfg.org_id or planning_cfg.org_id,
        previous_plan=previous_plan,
        current_plan=continuation if is_continuation else None,
        agent_name=agent.config.agent.name,
        goal=goal,
    )

    preload = ["ouro:update_post"] if is_continuation else ["ouro:create_post"]

    result = await agent.run(
        prompt,
        model_override=hb_model,
        mode=RunMode.PLAN,
        allowed_servers=servers,
        preload_tools=preload,
    )

    if is_continuation:
        cycle = continuation
    else:
        cycle = PlanCycle(status="pending_review", kind=kind, goal=goal)

    try:
        json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
        raw = json_match.group(1) if json_match else result
        parsed = json.loads(raw)
        cycle.plan_text = parsed.get("plan", "")
        if not is_continuation:
            cycle.post_id = parsed.get("post_id")
        raw_items = parsed.get("items", [])
        if is_continuation:
            seed = parse_plan_items(raw_items) if raw_items else cycle.items
            if parse_task_lines_from_markdown(cycle.plan_text):
                cycle.items = sync_plan_items_from_markdown(cycle.plan_text, seed)
            elif raw_items:
                cycle.items = parse_plan_items(raw_items)
            else:
                cycle.items = seed
        elif raw_items:
            cycle.items = parse_plan_items(raw_items)
        elif cycle.plan_text:
            cycle.items = sync_plan_items_from_markdown(cycle.plan_text, [])
        else:
            cycle.items = []
    except (json.JSONDecodeError, AttributeError):
        logger.warning(
            "Could not parse planning result as JSON, storing raw result"
        )
        cycle.plan_text = result

    if not is_continuation:
        cycle.status = "pending_review"

    plan_store.save(cycle)

    if cycle.post_id and not is_continuation:
        planning_cfg = agent.config.planning
        auto_at = None
        if planning_cfg.auto_approve:
            review_secs = parse_cadence_seconds(planning_cfg.review_window)
            if review_secs:
                activate_time = datetime.now(timezone.utc) + timedelta(seconds=review_secs)
                local_act = activate_time.astimezone()
                now_local = datetime.now().astimezone()
                if local_act.date() == now_local.date():
                    auto_at = local_act.strftime("%H:%M")
                else:
                    auto_at = local_act.strftime("%Y-%m-%d %H:%M")
        update_post_status(agent._get_ouro_client(), cycle, auto_activate_at=auto_at)

    kind_label = "goal " if cycle.kind == "goal" else ""
    action_label = "revised" if is_continuation else "created"
    logger.info(
        "Planning cycle %s %s%s (post_id=%s)", cycle.id, kind_label, action_label, cycle.post_id
    )
    post_link = f" [plan](asset:{cycle.post_id})" if cycle.post_id else ""
    write_daily_log(
        agent.config.agent.workspace,
        f"[planning:{action_label}]{post_link} {kind_label}plan {action_label}",
        doc_store=agent.doc_store,
        agent_name=agent.config.agent.name,
    )
    return result


async def run_review_heartbeat(
    agent: OuroAgent,
    hb_model,
    plan_store: PlanStore,
    current: Optional[PlanCycle],
    servers: list[str],
    inline_feedback: Optional[str] = None,
    reply_parent_id: str | None = None,
    thread_parent_id: str | None = None,
    prefetch=None,
) -> Optional[PlanCycle]:
    """Check for human feedback on the plan post and activate if reviewed.

    If *inline_feedback* is provided (e.g. from a webhook event), it is
    included directly in the prompt so the agent doesn't need to call
    get_comments.
    """
    from ..memory.reflection import write_daily_log
    from .profiles import RunMode

    if not current or not current.post_id:
        if current:
            current.status = "active"
            current.activated_at = datetime.now(timezone.utc).isoformat()
            plan_store.save(current)
            logger.info("Plan cycle %s activated (no post to review)", current.id)
            write_daily_log(
                agent.config.agent.workspace,
                "[planning:activated] Plan activated (no post)",
                doc_store=agent.doc_store,
                agent_name=agent.config.agent.name,
            )
        return current

    review_preload = [
        "ouro:get_comments", "ouro:create_comment", "ouro:update_post",
    ]

    if inline_feedback:
        prompt = build_feedback_review_prompt(
            post_id=current.post_id,
            plan_text=current.plan_text,
            feedback_text=inline_feedback,
            current_status=current.status,
            reply_parent_id=reply_parent_id,
            thread_parent_id=thread_parent_id,
        )
    else:
        prompt = build_review_prompt(
            post_id=current.post_id, plan_text=current.plan_text, current_status=current.status
        )

    result = await agent.run(
        prompt,
        model_override=hb_model,
        mode=RunMode.REVIEW,
        allowed_servers=servers,
        preload_tools=review_preload,
        prefetch=prefetch,
    )

    try:
        json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
        raw = json_match.group(1) if json_match else result
        parsed = json.loads(raw)
        feedback = parsed.get("feedback_summary")
        revised = parsed.get("revised_plan")
        next_status = parsed.get("next_status")
        if next_status not in {"active", "pending_review", "cancelled"}:
            next_status = "active" if current.status == "active" else "pending_review"

        if feedback:
            if next_status != "cancelled":
                current.plan_text = revised or current.plan_text
                current.items = sync_plan_items_from_markdown(
                    current.plan_text, current.items,
                )

            if next_status == "cancelled":
                current.human_feedback = feedback
                current.status = "cancelled"
                current.completed_at = datetime.now(timezone.utc).isoformat()
                archived = plan_store.archive(current, ouro_client=agent._get_ouro_client())
                logger.info("Plan cycle %s cancelled via feedback: %s", current.id, feedback[:100])
                post_link = (
                    f" [plan](asset:{current.post_id})" if current.post_id else ""
                )
                write_daily_log(
                    agent.config.agent.workspace,
                    f"[planning:cancelled]{post_link} Plan cancelled: {feedback[:100]}",
                    doc_store=agent.doc_store,
                    agent_name=agent.config.agent.name,
                )
                return archived

            if next_status == "active":
                current.human_feedback = feedback
                current.status = "active"
                current.activated_at = datetime.now(timezone.utc).isoformat()
                plan_store.save(current)
                desc = plan_post_description(current)
                _sync_post(
                    agent._get_ouro_client(), current.post_id,
                    current.items, current.plan_text, description=desc,
                )
                logger.info("Plan cycle %s activated with feedback", current.id)
                post_link = (
                    f" [plan](asset:{current.post_id})" if current.post_id else ""
                )
                write_daily_log(
                    agent.config.agent.workspace,
                    f"[planning:approved]{post_link} Plan approved: {feedback[:100]}",
                    doc_store=agent.doc_store,
                    agent_name=agent.config.agent.name,
                )
                return current
            if next_status == "pending_review":
                current.revision_count += 1
                plan_store.save(current)
                desc = plan_post_description(current)
                _sync_post(
                    agent._get_ouro_client(), current.post_id,
                    current.items, current.plan_text, description=desc,
                )
                if current.status == "active":
                    logger.info(
                        "Plan cycle %s revised while active: %s",
                        current.id, feedback[:100],
                    )
                else:
                    logger.info(
                        "Plan cycle %s revised (not yet approved): %s",
                        current.id, feedback[:100],
                    )
                post_link = (
                    f" [plan](asset:{current.post_id})" if current.post_id else ""
                )
                log_prefix = (
                    "Plan revised while active"
                    if current.status == "active"
                    else "Plan revised, pending approval"
                )
                write_daily_log(
                    agent.config.agent.workspace,
                    f"[planning:revised]{post_link} {log_prefix}: {feedback[:100]}",
                    doc_store=agent.doc_store,
                    agent_name=agent.config.agent.name,
                )
                return None
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Could not parse review result as JSON")

    return None
