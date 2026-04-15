"""Planning cycle: plan / review / execute layered on top of heartbeats.

The agent periodically generates a plan (published as an Ouro quest for human
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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from ..constants import _INTERVAL_RE, parse_json_from_llm

if TYPE_CHECKING:
    from ..agent import OuroAgent
    from ..teams import TeamContext

logger = logging.getLogger(__name__)


def _plan_doc_store(agent: "OuroAgent", team_id: str | None):
    if team_id:
        return agent.doc_store_for(team_id)
    return agent.doc_store


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
    status: Literal[
        "planning", "pending_review", "active", "completed", "cancelled"
    ] = "planning"
    kind: Literal["default", "goal"] = "default"
    goal: str = ""
    plan_text: str = ""
    items: list[PlanItem] = []
    quest_id: Optional[str] = None
    team_id: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    activated_at: Optional[str] = None
    completed_at: Optional[str] = None
    heartbeats_completed: int = 0
    human_feedback: Optional[str] = None
    revision_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_post_id(cls, values):
        """Accept legacy ``post_id`` from persisted JSON and map it to ``quest_id``."""
        if isinstance(values, dict) and "post_id" in values:
            values.setdefault("quest_id", values.pop("post_id"))
        return values

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

    When *team_id* is set, cycles saved through this store inherit it.
    """

    def __init__(self, plans_dir: Path, team_id: str | None = None):
        self._dir = plans_dir
        self._active_dir = plans_dir / "active"
        self._history_dir = plans_dir / "history"
        self.team_id = team_id
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
            if self.team_id and not cycle.team_id:
                cycle.team_id = self.team_id
            self.save(cycle)
            legacy.unlink(missing_ok=True)
            logger.info(
                "Migrated legacy current.json → active/%s", self._filename(cycle)
            )
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

    def load_by_quest_id(self, quest_id: str) -> Optional[PlanCycle]:
        """Find an active plan by its Ouro quest ID."""
        for cycle in self.load_all_active():
            if cycle.quest_id == quest_id:
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
        if self.team_id and not cycle.team_id:
            cycle.team_id = self.team_id
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

        If *ouro_client* is provided and the cycle has a quest_id, the Ouro
        quest is updated to reflect its final status.
        """
        if cycle.status not in ("completed", "cancelled"):
            cycle.status = "completed"
        cycle.completed_at = datetime.now(timezone.utc).isoformat()

        if ouro_client and cycle.quest_id:
            update_quest_status(ouro_client, cycle)

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
        files = sorted(
            self._history_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
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


def render_plan_items(items: list[PlanItem], include_ids: bool = True) -> str:
    """Render plan items as a status list for context injection."""
    lines: list[str] = []
    for item in items:
        marker = "x" if item.status in ("done", "skipped") else " "
        line = f"[{marker}] {item.description}"
        if include_ids:
            line += f" (item_id: {item.id})"
        if item.status == "in_progress":
            line += " [in_progress]"
        if item.notes:
            line += f" — {item.notes}"
        lines.append(line)
    return "\n".join(lines)


def render_numbered_plan_items(items: list[PlanItem], include_ids: bool = True) -> str:
    """Render plan items with explicit 1-indexed numbering for review prompts."""
    if not items:
        return ""
    base_lines = render_plan_items(items, include_ids=include_ids).splitlines()
    return "\n".join(
        f"{idx}. {line}" for idx, line in enumerate(base_lines, start=1) if line.strip()
    )


def render_plan_context(cycle: PlanCycle) -> str:
    """Build the structured plan context block injected into the heartbeat playbook."""
    total = len(cycle.items)
    done = cycle.items_done
    if cycle.kind == "goal" and cycle.goal:
        label = f"Goal Plan: {cycle.goal}"
    else:
        label = "Default Plan"
    parts = [f"## {label} (id: {cycle.id[:8]}, quest: {cycle.quest_id or 'n/a'})"]
    if total:
        parts.append(f"Progress: {done}/{total} items complete\n")
        parts.append(render_plan_items(cycle.items))
    elif cycle.plan_text:
        parts.append(cycle.plan_text)
    return "\n".join(parts)


def format_plans_index_for_prompt(plans: list[PlanCycle]) -> str:
    """Short list of plan Ouro quest ids for system prompts (no plan body).

    Lets the model call ``get_asset`` on a quest id when it needs the full plan.
    """

    lines: list[str] = []
    for p in plans:
        if p.status not in ("active", "pending_review") or not p.quest_id:
            continue
        lines.append(
            f"- `{p.quest_id}` — asset type: quest; plan kind: {p.kind}; "
            f"status: {p.status}; cycle_id: {p.id[:8]}"
        )
    if not lines:
        return ""
    return (
        "These quests hold plan content on the platform. "
        "Use `get_asset` with the quest id if you need the full text.\n\n"
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
    """Parse items from LLM JSON or API response into PlanItem instances."""
    items: list[PlanItem] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"description": entry}
        items.append(
            PlanItem(
                id=entry.get("id", str(uuid4())[:8]),
                description=entry.get("description", ""),
                status=entry.get("status", "pending"),
                notes=entry.get("notes", ""),
            )
        )
    return items


def refresh_items_from_api(ouro_client, quest_id: str) -> list[PlanItem]:
    """Fetch quest items from the API and return as PlanItems."""
    if not ouro_client or not quest_id:
        return []
    try:
        api_items = ouro_client.quests.list_items(quest_id)
        return [
            PlanItem(
                id=str(item.id) if item.id else str(uuid4())[:8],
                description=item.description,
                status=item.status,
                notes=item.notes or "",
            )
            for item in api_items
        ]
    except Exception as e:
        logger.warning("Failed to fetch items for quest %s: %s", quest_id, e)
        return []


def update_quest_status(ouro_client, cycle: PlanCycle, **kwargs) -> None:
    """Sync the platform quest lifecycle with the local planning cycle."""
    if not ouro_client or not cycle.quest_id:
        return
    try:
        update_kw: dict = {}
        if cycle.status in ("planning", "pending_review"):
            update_kw["status"] = "draft"
        elif cycle.status == "active":
            update_kw["status"] = "open"
        elif cycle.status == "completed":
            update_kw["status"] = "closed"
        elif cycle.status == "cancelled":
            update_kw["status"] = "cancelled"
        if update_kw:
            ouro_client.quests.update(cycle.quest_id, **update_kw)
    except Exception as e:
        logger.warning("Failed to update plan quest status %s: %s", cycle.quest_id, e)


def comment_on_plan(ouro_client, quest_id: str, markdown: str) -> None:
    """Post a comment on the plan quest to communicate status changes."""
    if not ouro_client or not quest_id:
        return
    try:
        content = ouro_client.quests.Content()
        content.from_markdown(markdown)
        ouro_client.comments.create(content=content, parent_id=quest_id)
    except Exception as e:
        logger.warning("Failed to comment on plan quest %s: %s", quest_id, e)


def notify_controller_plan_ready(
    ouro_client, quest_id: str, controller_username: str | None
) -> None:
    """Notify the configured controller that a plan is awaiting review."""
    if not controller_username:
        return
    username = controller_username.strip().lstrip("@")
    if not username:
        return
    comment_on_plan(
        ouro_client,
        quest_id,
        f"{{@{username}}} this plan is ready for review.",
    )


# ---------------------------------------------------------------------------
# Agent-facing tool
# ---------------------------------------------------------------------------


def make_plan_tools(plan_store: "PlanStore", ouro_client=None) -> list:
    """Deprecated: plan updates now use MCP quest item tools
    (complete_quest_item, update_quest_item, etc.)."""
    return []


# ---------------------------------------------------------------------------
# Interval parsing
# ---------------------------------------------------------------------------

from ..constants import parse_interval_seconds as parse_cadence_seconds  # noqa: E402

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
        if (
            current.all_items_complete
            and current.heartbeats_completed >= min_heartbeats
        ):
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

IMPORTANT: You MUST do both of the following steps. Do NOT skip the quest creation.
Do NOT use any tools besides create_quest and final_answer. Do NOT attempt to
execute any plan items or do actual work — only write and publish the plan.

Step 1. Call create_quest to publish your plan{quest_instructions}.
   Name it exactly "{quest_name}".
   - Pass status="draft" so the plan quest is not live until approved.
   - Pass description_markdown with **prose context**: background, reasoning,
     focus areas. Use headers and paragraphs — no checklists in the description.
   - Pass items as a list of specific, actionable task descriptions (strings).
     Each item becomes a trackable task on the platform.

Step 2. After create_quest succeeds, call final_answer with this JSON:
```json
{{"quest_id": "<the asset id from create_quest>"}}
```
"""

CONTINUATION_PLANNING_PROMPT_TEMPLATE = """\
You are revising your current plan.{goal_section}

Review what has happened, what's been completed, and what needs to change
for the upcoming period (~{cadence_description}).

## Current Items
{current_items_section}

{previous_plan_section}

{context_section}

You may add new items, remove items that are no longer relevant, or adjust priorities.
Items that are already done stay done.

IMPORTANT: Do NOT attempt to execute any plan items — only revise and update the plan.
Use only the tools listed below and final_answer.

Step 1. Update the plan description if context has changed:
   Call update_quest (id: {quest_id}){quest_instructions}.
   Only update description_markdown unless you intentionally need to change the
   quest lifecycle status. Pending-review plans should stay `draft`; approved
   plans should be `open`. Do NOT change the quest name.

Step 2. Manage items as needed:
   - Use the item_id values shown below when calling item tools.
   - create_quest_items(quest_id, items): batch-add new task descriptions
   - update_quest_item(quest_id, item_id, ...): change description, notes, or status
   - delete_quest_item(quest_id, item_id): remove irrelevant items (only if no entries)
   - If the item list below seems stale, call list_quest_items(quest_id) before editing.

Step 3. After all updates, call final_answer with this JSON:
```json
{{"quest_id": "{quest_id}"}}
```
"""

REVIEW_PROMPT_TEMPLATE = """\
You published a plan as an Ouro quest (asset ID: {quest_id}).
Check if there are any comments on that quest with feedback from a human reviewer
(use get_comments).

Current plan status: {current_status}

Your current plan:
{plan_text}

If there is feedback, revise your plan to incorporate it and update the quest
(update_quest).  Then reply to the reviewer with a comment (create_comment on the
quest) summarizing what you changed and what the plan's next status should be.

If there are no comments or the comments don't require changes, keep the plan as-is.

Do NOT execute any plan items — only review feedback and revise.

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
    team_context: Optional["TeamContext"] = None,
) -> str:
    """Build a planning or continuation-planning prompt.

    If *current_plan* has a quest_id, produces a continuation prompt that asks
    the LLM to update the existing quest.  Otherwise produces a fresh-plan
    prompt that asks the LLM to create a new quest.

    When *goal* is provided the plan is framed around achieving that goal.

    Accepts *team_context* (preferred) or raw *team_id*/*org_id* for backward
    compatibility.
    """
    if team_context:
        team_id = team_context.team_id
        org_id = team_context.org_id

    quest_parts = []
    if org_id:
        quest_parts.append(f'org_id="{org_id}"')
    if team_id:
        quest_parts.append(f'team_id="{team_id}"')
    quest_parts.append('visibility="public"')
    quest_instructions = f" (use {', '.join(quest_parts)})" if quest_parts else ""

    goal_section = ""
    if goal:
        goal_section = (
            f" The focus area for this planning period is:\n\n"
            f"> {goal}\n\n"
            f"Structure your plan around this focus area."
        )

    # Continuation planning: update existing quest
    if current_plan and current_plan.quest_id:
        current_items_section = (
            render_plan_items(current_plan.items, include_ids=True)
            if current_plan.items
            else current_plan.plan_text
        )

        previous_plan_section = ""
        if previous_plan and previous_plan.plan_text:
            previous_plan_section = (
                "## Previous Completed Plan\n" f"{previous_plan.plan_text}\n"
            )

        context_section = ""
        if extra_context:
            context_section = f"## Additional Context\n{extra_context}"

        return CONTINUATION_PLANNING_PROMPT_TEMPLATE.format(
            cadence_description=_cadence_description(cadence),
            current_items_section=current_items_section,
            quest_id=current_plan.quest_id,
            quest_instructions=quest_instructions,
            previous_plan_section=previous_plan_section,
            context_section=context_section,
            goal_section=goal_section,
        )

    # Fresh plan: create a new quest
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
        quest_name = (
            f"PLAN:{agent_name}:goal:{goal_slug}"
            if agent_name
            else f"PLAN:goal:{goal_slug}"
        )
    else:
        quest_name = (
            f"PLAN:{agent_name}:{date_label}" if agent_name else f"PLAN:{date_label}"
        )

    return PLANNING_PROMPT_TEMPLATE.format(
        cadence_description=_cadence_description(cadence),
        quest_instructions=quest_instructions,
        previous_plan_section=previous_plan_section,
        context_section=context_section,
        quest_name=quest_name,
        goal_section=goal_section,
    )


FEEDBACK_REVIEW_PROMPT_TEMPLATE = """\
You received direct feedback on your plan (Ouro quest ID: {quest_id}).

Current plan status: {current_status}

Current structured quest items (frontend numbering is 1-indexed):
{current_items_section}

Your current plan:
{plan_text}

Feedback received:
{feedback_text}

{thread_context}

Steps:
1. First interpret any "item N" references against the structured quest item list
   above, using its 1-indexed numbering. Do NOT infer item numbers from prose
   headings, markdown bullets, or the plan body.
2. Revise your plan to incorporate this feedback (if changes are needed).
3. Manage structured quest items directly as needed:
   - Use the item_id values shown above when calling item tools.
   - update_quest_item(quest_id, item_id, ...): change description, notes, status,
     or sort_order
   - delete_quest_item(quest_id, item_id): remove irrelevant items (only if no entries)
   - If you remove or reorder items, normalize sort_order to match the frontend's
     1-indexed numbering (1, 2, 3, ...).
   - If the list above seems stale, call list_quest_items(quest_id) before editing.
4. If you revised the prose plan body, update the quest (update_quest).
5. {reply_instruction} Summarize what you changed, and if the plan is not yet
   approved, note that you're awaiting their go-ahead before executing.

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


def build_review_prompt(quest_id: str, plan_text: str, current_status: str) -> str:
    return REVIEW_PROMPT_TEMPLATE.format(
        quest_id=quest_id,
        plan_text=plan_text,
        current_status=current_status,
    )


def build_feedback_review_prompt(
    quest_id: str,
    plan_text: str,
    current_items_section: str,
    feedback_text: str,
    current_status: str,
    reply_parent_id: str | None = None,
    thread_parent_id: str | None = None,
) -> str:
    if reply_parent_id and reply_parent_id != quest_id:
        reply_instruction = (
            f"Reply in the same comment thread by calling create_comment with "
            f"parent_id `{reply_parent_id}`."
        )
    else:
        reply_instruction = f"Reply on the plan quest by calling create_comment with parent_id `{quest_id}`."

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
            "Keep it active after incorporating feedback unless the reviewer explicitly "
            "asks you to pause, stop, or hold execution pending another review. In the "
            'normal case, set "next_status": "active" and reply as someone continuing an '
            "active plan, not as someone waiting for initial approval."
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
        quest_id=quest_id,
        current_status=current_status,
        current_items_section=current_items_section or "(no quest items)",
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

    If *continuation* is provided (an active cycle with a quest_id), the
    prompt asks the LLM to revise and update the existing quest.  Otherwise
    a fresh plan is created.

    When *goal* is given the plan is framed around achieving it.
    *kind* controls whether this is a ``"default"`` cadence plan or a
    ``"goal"`` plan that coexists alongside the default.
    """
    from ..memory.reflection import write_daily_log
    from .profiles import RunMode

    planning_cfg = agent.config.planning
    plan_model = (
        agent._build_model(planning_cfg.model, heartbeat=True)
        if planning_cfg.model
        else hb_model
    )
    previous = plan_store.load_history(limit=1)
    previous_plan = previous[0] if previous else None

    is_continuation = continuation and continuation.quest_id
    agent_cfg = agent.config.agent

    from ..teams import TeamContext as _TC

    tc: _TC | None = None
    if plan_store.team_id and agent_cfg.org_id:
        tc = _TC(team_id=plan_store.team_id, org_id=agent_cfg.org_id)
    active_doc_store = _plan_doc_store(agent, plan_store.team_id)

    prompt = build_planning_prompt(
        cadence=planning_cfg.cadence,
        previous_plan=previous_plan,
        current_plan=continuation if is_continuation else None,
        agent_name=agent_cfg.name,
        goal=goal,
        team_context=tc,
    )

    if is_continuation:
        preload = [
            "ouro:update_quest",
            "ouro:list_quest_items",
            "ouro:create_quest_items",
            "ouro:update_quest_item",
            "ouro:delete_quest_item",
        ]
    else:
        preload = ["ouro:create_quest"]

    result = await agent.run(
        prompt,
        model_override=plan_model,
        mode=RunMode.PLAN,
        allowed_servers=servers,
        preload_tools=preload,
        team_id=plan_store.team_id,
    )

    if is_continuation:
        cycle = continuation
    else:
        cycle = PlanCycle(status="pending_review", kind=kind, goal=goal)

    parsed = parse_json_from_llm(result)
    if parsed:
        cycle.plan_text = parsed.get("plan", cycle.plan_text or "")
        if not is_continuation:
            cycle.quest_id = parsed.get("quest_id") or parsed.get("post_id")
    else:
        logger.warning("Could not parse planning result as JSON, storing raw result")
        cycle.plan_text = result

    # Refresh items from the platform (source of truth)
    ouro = agent._get_ouro_client()
    if cycle.quest_id and ouro:
        cycle.items = refresh_items_from_api(ouro, cycle.quest_id)

    if not is_continuation:
        cycle.status = "pending_review"

    plan_store.save(cycle)
    if cycle.status == "pending_review" and cycle.quest_id:
        notify_controller_plan_ready(
            ouro,
            cycle.quest_id,
            agent.config.controller.username,
        )

    kind_label = "goal " if cycle.kind == "goal" else ""
    action_label = "revised" if is_continuation else "created"
    logger.info(
        "Planning cycle %s %s%s (quest_id=%s)",
        cycle.id,
        kind_label,
        action_label,
        cycle.quest_id,
    )
    quest_link = f" [plan](asset:{cycle.quest_id})" if cycle.quest_id else ""
    write_daily_log(
        agent.config.agent.workspace,
        f"[planning:{action_label}]{quest_link} {kind_label}plan {action_label}",
        doc_store=active_doc_store,
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
    """Check for human feedback on the plan quest and activate if reviewed.

    If *inline_feedback* is provided (e.g. from a webhook event), it is
    included directly in the prompt so the agent doesn't need to call
    get_comments.
    """
    from ..memory.reflection import write_daily_log
    from .profiles import RunMode

    active_doc_store = _plan_doc_store(agent, plan_store.team_id)
    ouro = agent._get_ouro_client()

    if not current or not current.quest_id:
        if current:
            current.status = "active"
            current.activated_at = datetime.now(timezone.utc).isoformat()
            plan_store.save(current)
            logger.info("Plan cycle %s activated (no quest to review)", current.id)
            write_daily_log(
                agent.config.agent.workspace,
                "[planning:activated] Plan activated (no quest)",
                doc_store=active_doc_store,
                agent_name=agent.config.agent.name,
            )
        return current

    review_preload = [
        "ouro:get_comments",
        "ouro:create_comment",
        "ouro:update_quest",
        "ouro:list_quest_items",
        "ouro:create_quest_items",
        "ouro:update_quest_item",
        "ouro:delete_quest_item",
    ]

    current_items = refresh_items_from_api(ouro, current.quest_id)
    if current_items:
        current.items = current_items
    current_items_section = (
        render_numbered_plan_items(current.items, include_ids=True)
        if current.items
        else "(no quest items)"
    )

    if inline_feedback:
        prompt = build_feedback_review_prompt(
            quest_id=current.quest_id,
            plan_text=current.plan_text,
            current_items_section=current_items_section,
            feedback_text=inline_feedback,
            current_status=current.status,
            reply_parent_id=reply_parent_id,
            thread_parent_id=thread_parent_id,
        )
    else:
        prompt = build_review_prompt(
            quest_id=current.quest_id,
            plan_text=current.plan_text,
            current_status=current.status,
        )

    planning_cfg = agent.config.planning
    plan_model = (
        agent._build_model(planning_cfg.model, heartbeat=True)
        if planning_cfg.model
        else hb_model
    )

    result = await agent.run(
        prompt,
        model_override=plan_model,
        mode=RunMode.REVIEW,
        allowed_servers=servers,
        preload_tools=review_preload,
        prefetch=prefetch,
        team_id=plan_store.team_id,
    )

    parsed = parse_json_from_llm(result)
    if parsed:
        feedback = parsed.get("feedback_summary")
        revised = parsed.get("revised_plan")
        next_status = parsed.get("next_status")
        if next_status not in {"active", "pending_review", "cancelled"}:
            next_status = "active" if current.status == "active" else "pending_review"

        if feedback:
            if next_status != "cancelled":
                current.plan_text = revised or current.plan_text
                if current.quest_id and ouro:
                    current.items = refresh_items_from_api(ouro, current.quest_id)

            if next_status == "cancelled":
                current.human_feedback = feedback
                current.status = "cancelled"
                current.completed_at = datetime.now(timezone.utc).isoformat()
                archived = plan_store.archive(current, ouro_client=ouro)
                logger.info(
                    "Plan cycle %s cancelled via feedback: %s",
                    current.id,
                    feedback[:100],
                )
                quest_link = (
                    f" [plan](asset:{current.quest_id})" if current.quest_id else ""
                )
                write_daily_log(
                    agent.config.agent.workspace,
                    f"[planning:cancelled]{quest_link} Plan cancelled: {feedback[:100]}",
                    doc_store=active_doc_store,
                    agent_name=agent.config.agent.name,
                )
                return archived

            if next_status == "active":
                current.human_feedback = feedback
                current.status = "active"
                current.activated_at = datetime.now(timezone.utc).isoformat()
                plan_store.save(current)
                update_quest_status(ouro, current)
                logger.info("Plan cycle %s activated with feedback", current.id)
                quest_link = (
                    f" [plan](asset:{current.quest_id})" if current.quest_id else ""
                )
                write_daily_log(
                    agent.config.agent.workspace,
                    f"[planning:approved]{quest_link} Plan approved: {feedback[:100]}",
                    doc_store=active_doc_store,
                    agent_name=agent.config.agent.name,
                )
                return current
            if next_status == "pending_review":
                current.revision_count += 1
                plan_store.save(current)
                if current.status != "active":
                    update_quest_status(ouro, current)
                if current.status == "active":
                    logger.info(
                        "Plan cycle %s revised while active: %s",
                        current.id,
                        feedback[:100],
                    )
                else:
                    logger.info(
                        "Plan cycle %s revised (not yet approved): %s",
                        current.id,
                        feedback[:100],
                    )
                quest_link = (
                    f" [plan](asset:{current.quest_id})" if current.quest_id else ""
                )
                log_prefix = (
                    "Plan revised while active"
                    if current.status == "active"
                    else "Plan revised, pending approval"
                )
                write_daily_log(
                    agent.config.agent.workspace,
                    f"[planning:revised]{quest_link} {log_prefix}: {feedback[:100]}",
                    doc_store=active_doc_store,
                    agent_name=agent.config.agent.name,
                )
                return None
    else:
        logger.warning("Could not parse review result as JSON")

    return None
