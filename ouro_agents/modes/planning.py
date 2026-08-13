"""Planning: periodic quest creation and draft auto-approval.

A plan is just a quest. The platform is the single source of truth for plan
content, item status, and lifecycle (``draft`` → ``open`` → ``closed``); the
only local state is a tiny per-team cursor recording when the agent last
planned and which quests it published.

The planning loop has two moving parts, both driven from the heartbeat:

- **Creation**: when the cadence comes due and the work inbox has drained,
  a planning run publishes a new draft quest scoped to one focus.
- **Approval**: draft quests auto-promote to ``open`` after the review window
  elapses (when ``auto_approve`` is set). Comments on quests use the normal
  autonomous comment path — there is no dedicated review run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel

from ..constants import _INTERVAL_RE, clip_text, parse_json_from_llm
from ..constants import parse_interval_seconds as parse_cadence_seconds
from ..memory.focus import build_focus_memory_context, remember_work_direction
from ..syncing import normalize_status, read_field
from ..tool_preloads import PLANNING

if TYPE_CHECKING:
    from ..agent import OuroAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Planning cursor — the only local planning state
# ---------------------------------------------------------------------------


class PlanningCursor(BaseModel):
    """Per-team planning bookkeeping stored at ``teams/<id>/planning.json``.

    ``pending_quest_ids`` tracks draft quests this agent published and is the
    auto-approval worklist; entries are removed once a quest leaves ``draft``
    (or disappears from the platform).
    """

    last_planned_at: str = ""
    last_quest_id: str = ""
    pending_quest_ids: list[str] = []


def _cursor_path(workspace: Path, team_id: str) -> Path:
    from ..memory.team_paths import team_workspace_dir

    return team_workspace_dir(workspace, team_id) / "planning.json"


def load_cursor(workspace: Path, team_id: str) -> PlanningCursor:
    path = _cursor_path(workspace, team_id)
    if not path.exists():
        return PlanningCursor()
    try:
        return PlanningCursor(**json.loads(path.read_text()))
    except Exception:
        logger.warning("Failed to load planning cursor %s", path)
        return PlanningCursor()


def save_cursor(workspace: Path, team_id: str, cursor: PlanningCursor) -> None:
    from ..memory_lock import memory_write_lock

    with memory_write_lock():
        path = _cursor_path(workspace, team_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cursor.model_dump(), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def planning_due(
    cursor: PlanningCursor, cadence: str, now: Optional[datetime] = None
) -> bool:
    """True when at least one cadence interval has passed since the last plan."""
    cadence_secs = parse_cadence_seconds(cadence)
    if not cadence_secs:
        return False
    last = _parse_iso_datetime(cursor.last_planned_at)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() >= cadence_secs


# ---------------------------------------------------------------------------
# Quest item helpers (API objects or dicts)
# ---------------------------------------------------------------------------


def _parse_iso_datetime(value: object) -> Optional[datetime]:
    """Best-effort parse of an ISO 8601 timestamp into an aware datetime."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def item_is_waiting(item: Any, now: Optional[datetime] = None) -> bool:
    """True when a quest item (dict or API object) is parked and not actionable.

    An unfinished item with a future ``waiting_until`` (or a ``waiting_on``
    reason and no date) is treated as waiting so it stays out of the work
    inbox until it comes due. A recurring check (``waiting_check_every``) with
    no next-time set is due now.
    """
    status = read_field(item, "status")
    if status in ("done", "skipped"):
        return False
    deadline = _parse_iso_datetime(read_field(item, "waiting_until"))
    if deadline is not None:
        now = now or datetime.now(timezone.utc)
        return deadline > now
    if read_field(item, "waiting_check_every"):
        return False
    waiting_on = read_field(item, "waiting_on") or ""
    return bool(str(waiting_on).strip())


def item_is_open(item: Any) -> bool:
    return read_field(item, "status") in ("pending", "in_progress")


def normalize_item(item: Any) -> dict[str, Any]:
    """Convert a QuestItem-like API object or dict into a plain dict."""
    if isinstance(item, dict):
        data = dict(item)
    else:
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            try:
                data = dump(mode="json")
            except TypeError:
                data = dump()
        else:
            data = dict(getattr(item, "__dict__", {}) or {})
        # SDK models may drop backend-only fields (waiting metadata); pull
        # them through explicitly when reachable.
        for key in ("waiting_on", "waiting_until", "waiting_check_every"):
            if key not in data:
                value = read_field(item, key)
                if value:
                    data[key] = value
    data["id"] = str(data.get("id") or "")
    data["quest_id"] = str(data.get("quest_id") or "")
    data.setdefault("description", "")
    data.setdefault("status", "pending")
    return data


def quest_status(quest: Any) -> str:
    """Normalized lifecycle status of a quest API object."""
    return normalize_status(
        read_field(quest, "quest.status") or read_field(quest, "status")
    )


def quest_description_text(quest: Any) -> str:
    """Extract the quest description as plain text/markdown."""
    description = read_field(quest, "description")
    if isinstance(description, str):
        return description
    text = read_field(description, "text") if description else None
    return str(text or "")


def quest_items(quest: Any) -> list[dict[str, Any]]:
    quest_id = str(read_field(quest, "id") or "")
    items = []
    for item in read_field(quest, "items", []) or []:
        data = normalize_item(item)
        data["quest_id"] = data["quest_id"] or quest_id
        items.append(data)
    return items


def render_quest_items(items: list[dict[str, Any]], include_ids: bool = True) -> str:
    """Render quest items as a status list for prompt context."""
    lines: list[str] = []
    for item in items:
        status = item.get("status") or "pending"
        marker = "x" if status in ("done", "skipped") else " "
        line = f"[{marker}] {item.get('description') or '(no description)'}"
        if include_ids and item.get("id"):
            line += f" (item_id: {item['id']})"
        if item_is_waiting(item):
            detail = str(item.get("waiting_on") or "").strip() or "external event"
            cadence = (
                f", checks every {item['waiting_check_every']}"
                if item.get("waiting_check_every")
                else ""
            )
            if item.get("waiting_until"):
                line += f" [waiting on {detail} until {item['waiting_until']}{cadence}]"
            else:
                line += f" [waiting on {detail}{cadence}]"
        elif status == "in_progress":
            line += " [in_progress]"
        if item.get("notes"):
            line += f" — {item['notes']}"
        lines.append(line)
    return "\n".join(lines)


def render_numbered_quest_items(
    items: list[dict[str, Any]], include_ids: bool = True
) -> str:
    """Render quest items with explicit 1-indexed numbering for review prompts."""
    if not items:
        return ""
    base_lines = render_quest_items(items, include_ids=include_ids).splitlines()
    return "\n".join(
        f"{idx}. {line}" for idx, line in enumerate(base_lines, start=1) if line.strip()
    )


# ---------------------------------------------------------------------------
# Own-quest discovery
# ---------------------------------------------------------------------------


def search_own_quests(agent: "OuroAgent", limit: int = 20) -> list[dict[str, Any]]:
    """Search the agent's own quests (newest activity first) as plain dicts."""
    own_user_id = getattr(agent, "own_user_id", None)
    if not own_user_id:
        return []
    try:
        ouro = agent._get_ouro_client()
        search = getattr(getattr(ouro, "assets", None), "search", None)
        if not search:
            return []
        kwargs: dict[str, Any] = {
            "asset_type": "quest",
            "user_id": str(own_user_id),
            "sort": "updated",
            "limit": limit,
        }
        org_id = getattr(agent.config.agent, "org_id", None)
        if org_id:
            kwargs["org_id"] = str(org_id)
        raw = search(**kwargs)
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("results") or []
        return [asset for asset in raw or [] if isinstance(asset, dict)]
    except Exception as e:
        logger.warning("Failed to search own quests: %s", e)
        return []


def format_quests_index_for_prompt(quests: list[dict[str, Any]]) -> str:
    """Short list of the agent's own quest ids for system prompts.

    Lets the model call ``get_asset`` on a quest id when it needs details.
    """
    lines: list[str] = []
    for quest in quests:
        quest_id = str(quest.get("id") or "")
        if not quest_id:
            continue
        name = str(quest.get("name") or "Untitled quest")
        team = str(quest.get("team_id") or "")
        line = f"- `{quest_id}` — {name}"
        if team:
            line += f" (team: {team})"
        lines.append(line)
    if not lines:
        return ""
    return (
        "Your own quests on the platform (newest activity first). "
        "Use `get_asset` with a quest id when you need its full plan and items.\n\n"
        + "\n".join(lines)
    )


def format_quests_index_pointer(quests: list[dict[str, Any]]) -> str:
    """One-line pointer for chat — avoid dumping quest titles that steer the thread."""
    n = sum(1 for q in quests if str(q.get("id") or "").strip())
    if n == 0:
        return ""
    noun = "quest" if n == 1 else "quests"
    return (
        f"{n} active plan {noun} on the platform. "
        "Use `search_assets` / `list_quest_items` / `get_asset` when you need plan details — "
        "do not treat the mere existence of plans as this conversation's goal."
    )


# ---------------------------------------------------------------------------
# Quest lifecycle helpers
# ---------------------------------------------------------------------------


def comment_on_quest(ouro_client, quest_id: str, markdown: str) -> None:
    """Post a comment on a quest to communicate status changes."""
    if not ouro_client or not quest_id:
        return
    try:
        content = ouro_client.quests.Content()
        content.from_markdown(markdown)
        ouro_client.comments.create(content=content, parent_id=quest_id)
    except Exception as e:
        logger.warning("Failed to comment on quest %s: %s", quest_id, e)


def notify_controller_quest_ready(
    ouro_client, quest_id: str, controller_username: str | None
) -> None:
    """Notify the configured controller that a plan quest awaits review."""
    if not controller_username:
        return
    username = controller_username.strip().lstrip("@")
    if not username:
        return
    comment_on_quest(
        ouro_client,
        quest_id,
        # Backtick-wrapped `{@username}` is the only form Ouro's markdown parser
        # turns into a real mention/notification (see ouro-js markdown-parser).
        f"`{{@{username}}}` this quest is ready for review.",
    )


def set_quest_status(ouro_client, quest_id: str, status: str) -> bool:
    """Move a quest's lifecycle status. Returns True on success."""
    if not ouro_client or not quest_id:
        return False
    try:
        ouro_client.quests.update(quest_id, status=status)
        return True
    except Exception as e:
        logger.warning("Failed to set quest %s status=%s: %s", quest_id, status, e)
        return False


def auto_approve_due_drafts(
    agent: "OuroAgent",
    team_ids: list[str],
    now: Optional[datetime] = None,
) -> int:
    """Promote cursor-tracked draft quests to ``open`` after the review window.

    Only quests the planning loop itself published (tracked in each team's
    cursor) are eligible — a draft someone deliberately parked never
    auto-opens. Quests that already left ``draft`` (or were deleted) are
    dropped from the pending list. Returns the number of quests opened.
    """
    planning_cfg = agent.config.planning
    if not planning_cfg.auto_approve:
        return 0
    review_secs = parse_cadence_seconds(planning_cfg.review_window)
    if not review_secs:
        return 0

    workspace = agent.config.agent.workspace
    now = now or datetime.now(timezone.utc)
    ouro = agent._get_ouro_client()
    opened = 0

    for team_id in team_ids:
        cursor = load_cursor(workspace, team_id)
        if not cursor.pending_quest_ids:
            continue
        still_pending: list[str] = []
        for quest_id in cursor.pending_quest_ids:
            try:
                quest = ouro.quests.retrieve(quest_id)
            except Exception as e:
                logger.info(
                    "Dropping unreachable pending quest %s from cursor: %s",
                    quest_id[:8],
                    e,
                )
                continue
            status = quest_status(quest)
            if status != "draft":
                continue
            created = _parse_iso_datetime(read_field(quest, "created_at"))
            if created is not None and (now - created).total_seconds() < review_secs:
                still_pending.append(quest_id)
                continue
            if set_quest_status(ouro, quest_id, "open"):
                comment_on_quest(
                    ouro,
                    quest_id,
                    "Review window elapsed with no feedback — plan auto-activated.",
                )
                opened += 1
                logger.info("Auto-approved plan quest %s", quest_id[:8])
            else:
                still_pending.append(quest_id)
        if still_pending != cursor.pending_quest_ids:
            cursor.pending_quest_ids = still_pending
            save_cursor(workspace, team_id, cursor)
    return opened


def remember_plan_feedback_direction(
    agent: "OuroAgent",
    quest_id: str,
    team_id: str | None,
    feedback: str | None,
) -> None:
    """Store plan-review feedback that should influence future planning."""
    agent_cfg = getattr(getattr(agent, "config", None), "agent", None)
    agent_name = getattr(agent_cfg, "name", "")
    if not agent_name:
        return
    from ..run_context import get_run_context

    active = get_run_context()
    run_id = (active.run_id if active else None) or quest_id
    remember_work_direction(
        getattr(agent, "memory", None),
        agent_name,
        feedback,
        source=f"plan-feedback:{quest_id}",
        run_id=run_id,
        team_id=team_id,
        asset_id=quest_id,
        strength=0.8,
        text_prefix="Planning guidance from review feedback",
    )


# ---------------------------------------------------------------------------
# Planning context builders
# ---------------------------------------------------------------------------


def build_previous_quest_context(ouro_client, quest_id: str) -> str:
    """Item-level outcome of the last planning quest, for fresh planning runs.

    Showing the previous quest's items with statuses and waiting metadata lets
    the model scope the new plan as a distinct focus instead of silently
    duplicating open threads.
    """
    if not ouro_client or not quest_id:
        return ""
    try:
        quest = ouro_client.quests.retrieve(quest_id)
    except Exception as e:
        logger.debug("Failed to fetch previous plan quest %s: %s", quest_id, e)
        return ""

    items = quest_items(quest)
    done = sum(1 for i in items if i.get("status") in ("done", "skipped"))
    parts = [
        "## Previous Plan Outcome",
        f"Quest `{quest_id}` — {read_field(quest, 'name') or 'Untitled'} "
        f"(status: {quest_status(quest) or 'unknown'}); "
        f"{done}/{len(items)} items resolved.",
    ]
    description = quest_description_text(quest)
    if description:
        parts.append(f"\nPlan notes:\n{description}")
    if items:
        parts.append(f"\nItems:\n{render_quest_items(items, include_ids=False)}")
    if any(item_is_open(i) for i in items):
        parts.append(
            "\nThe unfinished items above stay tracked on their own quest, which "
            "remains open until they resolve — you'll keep advancing them as open "
            "quest work, so they are not lost. Do NOT copy them into this plan. "
            "Scope this plan to a distinct new focus; only reference them here if "
            "this plan genuinely depends on them."
        )
    return "\n".join(parts)


def build_recent_activity_context(
    agent: "OuroAgent", team_id: str | None, limit: int = 8
) -> str:
    """Digest of recent runs from the run log, so planning isn't blind.

    Read-only tools are available during planning, but the digest keeps the
    common case cheap: the model shouldn't have to search for its own recent
    work. Failures are non-fatal — planning proceeds without the digest.
    """
    run_log = getattr(agent, "_run_log", None)
    query_runs = getattr(run_log, "query_runs", None)
    if not callable(query_runs):
        return ""
    try:
        kwargs: dict = {"limit": limit}
        if team_id:
            kwargs["team_id"] = team_id
        rows = query_runs(**kwargs)
    except Exception as e:
        logger.debug("Failed to build recent-activity context: %s", e)
        return ""

    lines: list[str] = []
    for row in rows or []:
        mode = row.get("mode") or "run"
        if mode in ("plan", "review"):
            continue
        started = str(row.get("started_at") or "")[:16]
        status = row.get("status") or "unknown"
        task = clip_text(row.get("task"), 140)
        result = clip_text(row.get("result"), 200)
        line = f"- [{started}] {mode} ({status}): {task}"
        if result:
            line += f" → {result}"
        lines.append(line)
    if not lines:
        return ""
    return (
        "## Recent Activity\n"
        "Your most recent runs, newest first. Use these as evidence of what "
        "you actually did and how it went — not as direction by themselves.\n"
        + "\n".join(lines)
    )


def build_quest_history_context(
    agent: "OuroAgent", limit: int = 15
) -> str:
    """Compact digest of the agent's own recent quests across teams.

    Lets planning see structural repetition that a single previous-quest
    retrospective cannot surface.
    """
    ouro = None
    try:
        ouro = agent._get_ouro_client()
    except Exception:
        return ""
    if not ouro:
        return ""

    assets = search_own_quests(agent, limit=limit)
    if not assets:
        return ""

    lines: list[str] = [
        "## Your Recent Quests",
        "Newest activity first. If your proposed plan structurally repeats "
        "these (same item shapes with a swapped domain), take a different "
        "approach or decline to plan. Do not reuse item phrasing from this "
        "digest.",
    ]
    for asset in assets:
        quest_id = str(asset.get("id") or "")
        if not quest_id:
            continue
        name = str(asset.get("name") or "Untitled")
        team_id = str(asset.get("team_id") or "")
        created = str(asset.get("created_at") or "")[:10]
        try:
            quest = ouro.quests.retrieve(quest_id)
        except Exception:
            lines.append(
                f"- `{quest_id}` — {name} ({created or 'unknown date'}"
                f"{f', team {team_id[:8]}' if team_id else ''}): "
                "(could not load items)"
            )
            continue
        items = quest_items(quest)
        done = sum(1 for i in items if i.get("status") in ("done", "skipped"))
        status = quest_status(quest) or "unknown"
        item_bits = [
            clip_text(i.get("description"), 90) for i in items[:6]
        ]
        items_line = "; ".join(item_bits) if item_bits else "(no items)"
        if len(items) > 6:
            items_line += f"; …(+{len(items) - 6} more)"
        lines.append(
            f"- `{quest_id}` — {name} [{status}] "
            f"({created or 'unknown date'}"
            f"{f', team {team_id[:8]}' if team_id else ''}; "
            f"{done}/{len(items)} resolved): {items_line}"
        )
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standing planning guidance (PLANNING.md)
# ---------------------------------------------------------------------------

PLANNING_MD_NAME = "PLANNING.md"
# ~4k tokens. Loose on purpose: controller feedback is the highest-leverage
# steering channel; dream/reflection is expected to consolidate it over time
# rather than the loader truncating it.
PLANNING_MD_MAX_CHARS = 16000


def planning_md_path(workspace: Path) -> Path:
    return workspace / PLANNING_MD_NAME


def load_planning_guidance(workspace: Path) -> str:
    """Return PLANNING.md body, truncated to the standing-guidance budget."""
    path = planning_md_path(workspace)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
        return ""
    if not text:
        return ""
    if len(text) > PLANNING_MD_MAX_CHARS:
        text = text[: PLANNING_MD_MAX_CHARS - 1] + "…"
    return (
        "## Standing Planning Guidance\n"
        "Binding policy from controller feedback. Follow these constraints "
        "when scoping plans. Dream/reflection should consolidate this file "
        "rather than silently discarding older bullets.\n\n"
        f"{text}"
    )


def append_planning_guidance(
    workspace: Path,
    feedback: str,
    *,
    source: str = "controller",
    when: Optional[datetime] = None,
) -> bool:
    """Append a timestamped controller-feedback bullet to PLANNING.md."""
    text = (feedback or "").strip()
    if not text:
        return False
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y-%m-%d")
    bullet = f"- [{stamp}] ({source}) {text}"
    path = planning_md_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
        if not existing.strip():
            header = (
                "# Standing Planning Guidance\n\n"
                "Controller feedback that should shape every planning run. "
                "Consolidate via dream/reflection; do not silently truncate.\n\n"
            )
            path.write_text(header + bullet + "\n", encoding="utf-8")
        else:
            sep = "" if existing.endswith("\n") else "\n"
            path.write_text(existing + sep + bullet + "\n", encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("Failed to append planning guidance: %s", e)
        return False


PLAN_ITEM_QUALITY_BAR = """\
Every plan item must pass this bar:
- It names a concrete deliverable or observable outcome (an asset created or
  updated, a route executed with results recorded, a substantive comment, a
  measurement taken) — not an activity. Rewrite vague items like "explore X"
  or "look into Y" as the artifact the exploration should produce.
- It is sized to roughly one heartbeat work session. Split anything bigger.
- Its done-condition is checkable from the item text alone: a reviewer should
  be able to tell from the platform whether it happened.
- The plan description must state explicitly what is *different* from your
  recent quests (see Your Recent Quests). At least one item must be a work
  type not present in that history digest — do not ship the same pipeline
  with only the domain swapped.
- Do not reuse item phrasing from recent quests. If your proposed items
  structurally repeat recent quests (same shapes, same post→email conveyor),
  either take a genuinely different approach or decline to plan (skip path).
- When later work depends on earlier findings, prefer 2–3 concrete items plus
  an explicit **checkpoint item** whose deliverable is revising the quest
  itself (add/rewrite remaining items and leave a comment explaining the
  pivot) over 4 pre-scripted deliverables. No vague "TBD" placeholders —
  the checkpoint's done-condition is checkable (items changed + rationale
  comment exists)."""

PLANNING_PROMPT_TEMPLATE = """\
You are entering a planning phase.{goal_section}

Your previous plan's outcome, recent quests, outcome evidence, standing
planning guidance, recent activity, and work-direction memory are included
below. You may also use read-only tools (search_assets, get_asset,
get_comments, list_quest_items) to inspect specific assets or threads before
scoping items — but keep inspection brief and targeted; most of what you need
is already here. Then create a plan for the upcoming period — or decline to
plan when that is the better call.

Your plan should be realistic given the time available ({budget_description}),
specific enough to guide your heartbeats, and flexible enough to adapt.
If the context includes work-direction guidance or Standing Planning Guidance,
treat them as binding constraints when choosing focus, scope, and negative
constraints before inventing new work.
Ground every focus choice in the current user goal, an approved quest item,
work-direction memory, standing guidance, or explicit evidence you have
evaluated. Recent platform activity alone is not a reason to prioritize a topic.

{item_quality_bar}

{previous_plan_section}

{context_section}

Allowed write tools: create_quest, create_quest_items, update_quest.
Do NOT attempt to execute any plan items or do actual work — only write and
publish the plan, or skip.

Default to publishing a plan. You may skip only when:
- Open quests still have real backlog (including waiting/parked items) that
  clearly should be worked before inventing new focus, or
- Standing Planning Guidance explicitly forbids a new cycle right now.
Do not skip because recent quests got little external engagement — treat that
as a signal to change approach in the new plan, not a reason to decline.
If you skip, do NOT call create_quest. End the turn with only this JSON:
```json
{{"quest_id": null, "skip_reason": "<brief reason>"}}
```

Otherwise do both of the following steps. Do NOT skip quest creation unless
using the skip path above.

Step 1. Call create_quest exactly once to publish your plan{quest_instructions}.
   {quest_name_instruction}
   - Pass status="draft" so the plan quest is not live until approved.
   - Pass description_markdown with **prose context**: background, reasoning,
     focus areas. Use headers and paragraphs — no checklists in the description.
   - Open the description with a short retrospective (2-3 sentences) that notes
     outcomes (did anyone respond, use, or build on the work?) as well as item
     completion. If engagement was weak, say so briefly and state how this plan
     changes approach — then still publish.
   - Explicitly state what this plan does differently from recent quests.
   - Pass items as a list of specific, actionable task descriptions (strings).
     Each item becomes a trackable task on the platform.
   - Call create_quest alone (not in parallel with inspection tools) so its
     result is unambiguous. Finish any read-only inspection first.
   - A create_quest result with an `id` field is success even if items look
     incomplete — fix that quest in place with create_quest_items / update_quest.
     Do NOT call create_quest again — never publish a second plan quest in the
     same run.

Step 2. After the plan quest is ready, end the turn with a final message that is only this JSON:
```json
{{"quest_id": "<the asset id from create_quest>"}}
```
"""


def _plan_quest_name_instruction(goal: str = "") -> str:
    """Instruction for natural plan quest titles."""
    if goal:
        return (
            "Name it with a concise, natural title that is just the goal or focus "
            "area. Do not include generic planning labels, dates, agent names, "
            "team names, or internal keys."
        )
    return (
        "Name it with a concise, natural title for the current focus in your own "
        "words. Do not include generic planning labels, dates, agent names, team "
        "names, or internal keys."
    )


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


def expected_heartbeats(cadence: str, heartbeat_every: str) -> Optional[int]:
    """How many heartbeat work sessions fit in one planning cadence."""
    cadence_secs = parse_cadence_seconds(cadence)
    beat_secs = parse_cadence_seconds(heartbeat_every)
    if not cadence_secs or not beat_secs:
        return None
    return max(1, cadence_secs // beat_secs)


def _budget_description(cadence: str, heartbeat_every: str = "") -> str:
    """Cadence plus, when derivable, the concrete heartbeat budget.

    'roughly N work sessions' is a far stronger sizing constraint than a raw
    duration, so include it whenever the heartbeat interval is known.
    """
    base = f"~{_cadence_description(cadence)}"
    beats = expected_heartbeats(cadence, heartbeat_every) if heartbeat_every else None
    if beats is None:
        return base
    return (
        f"{base}, roughly {beats} heartbeat work session{'s' if beats != 1 else ''} "
        "— plan about that many items, each sized to one session"
    )


def build_planning_prompt(
    cadence: str,
    team_id: Optional[str] = None,
    org_id: Optional[str] = None,
    previous_plan_section: str = "",
    extra_context: str = "",
    goal: str = "",
    heartbeat_every: str = "",
) -> str:
    """Build a fresh-plan prompt that asks the LLM to create a new quest.

    Each planning run produces its own newly-scoped quest; the agent never
    appends the next focus onto a prior quest. When *goal* is provided the
    plan is framed around achieving that goal.
    """
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

    context_section = ""
    if extra_context:
        context_section = f"## Additional Context\n{extra_context}"

    return PLANNING_PROMPT_TEMPLATE.format(
        budget_description=_budget_description(cadence, heartbeat_every),
        item_quality_bar=PLAN_ITEM_QUALITY_BAR,
        quest_instructions=quest_instructions,
        previous_plan_section=previous_plan_section,
        context_section=context_section,
        quest_name_instruction=_plan_quest_name_instruction(goal),
        goal_section=goal_section,
    )


# ---------------------------------------------------------------------------
# Planning run — create a new plan quest
# ---------------------------------------------------------------------------


def _plan_doc_store(agent: "OuroAgent", team_id: str | None):
    if team_id:
        return agent.doc_store_for(team_id)
    return agent.doc_store


def resolve_planning_model(agent: "OuroAgent", hb_model=None):
    """Build the planning model, falling back to *hb_model* if needed."""
    planning_cfg = agent.config.planning
    tier_model = None
    if hasattr(agent, "_model_id_for_role"):
        tier_model = agent._model_id_for_role("planning")
    plan_model_id = (
        planning_cfg.model
        or tier_model
        or getattr(hb_model, "model_id", None)
    )
    if plan_model_id:
        return agent._build_model(plan_model_id, role="planning")
    return hb_model


async def run_planning_run(
    agent: "OuroAgent",
    hb_model,
    team_id: str,
    servers: list[str],
    goal: str = "",
) -> Optional[str]:
    """Run a planning run: publish a fresh, newly-scoped draft quest.

    Every planning run creates its own quest — the agent never appends the
    next focus onto a prior quest. When *goal* is given the plan is framed
    around achieving it. The team cursor records the new quest for the
    auto-approval loop. The model may skip with ``quest_id: null``; the
    cursor's ``last_planned_at`` still advances so the cadence does not
    immediately re-fire.
    """
    from ..memory.reflection import write_log
    from .outcomes import build_outcome_evidence_context
    from .profiles import RunMode

    plan_model = resolve_planning_model(agent, hb_model)
    planning_cfg = agent.config.planning
    agent_cfg = agent.config.agent
    workspace = agent_cfg.workspace
    ouro = agent._get_ouro_client()

    cursor = load_cursor(workspace, team_id)
    previous_section = build_previous_quest_context(ouro, cursor.last_quest_id)

    direction_context = build_focus_memory_context(
        getattr(agent, "memory", None),
        agent_cfg.name,
        team_id=team_id,
        heading="Work Direction Guidance",
        guidance=(
            "Use these memories as strong input when choosing focus and task "
            "scope. If they conflict with the explicit goal for this planning "
            "run, prefer the explicit goal."
        ),
    )
    standing_context = ""
    controller_manager = getattr(agent, "_controller_questions", None)
    standing_fn = getattr(controller_manager, "standing_decisions_context", None)
    if callable(standing_fn):
        try:
            standing_context = standing_fn() or ""
        except Exception:
            logger.debug(
                "Failed to load standing controller decisions", exc_info=True
            )
    if standing_context:
        standing_context = (
            f"{standing_context}\n"
            "Settled controller decisions bind this plan: do not scope items "
            "that revisit or contradict them. Do not plan work that pre-empts "
            "a pending decision."
        )

    activity_context = build_recent_activity_context(agent, team_id)
    history_context = build_quest_history_context(agent)
    outcome_context = build_outcome_evidence_context(agent)
    guidance_context = load_planning_guidance(workspace)
    extra_context = "\n\n".join(
        part
        for part in (
            guidance_context,
            standing_context,
            history_context,
            outcome_context,
            direction_context,
            activity_context,
        )
        if part
    )

    prompt = build_planning_prompt(
        cadence=planning_cfg.cadence,
        team_id=team_id,
        org_id=getattr(agent_cfg, "org_id", None),
        previous_plan_section=previous_section,
        extra_context=extra_context,
        goal=goal,
        heartbeat_every=getattr(getattr(agent.config, "heartbeat", None), "every", "")
        or "",
    )

    result = await agent.run(
        prompt,
        model_override=plan_model,
        mode=RunMode.PLAN,
        allowed_servers=servers,
        preload_tools=list(PLANNING),
        team_id=team_id,
    )

    parsed = parse_json_from_llm(result)
    quest_id_raw = (parsed or {}).get("quest_id")
    skip_reason = str((parsed or {}).get("skip_reason") or "").strip()

    # Explicit skip: advance cadence cursor, do not publish.
    if parsed is not None and (quest_id_raw is None or quest_id_raw == ""):
        cursor.last_planned_at = datetime.now(timezone.utc).isoformat()
        save_cursor(workspace, team_id, cursor)
        reason = skip_reason or "no novel focus worth committing"
        logger.info("Planning run skipped for team %s: %s", team_id[:8], reason)
        write_log(
            workspace,
            f"[planning:skipped] {reason}",
            doc_store=_plan_doc_store(agent, team_id),
            agent_name=agent_cfg.name,
        )
        return result

    quest_id = str(quest_id_raw or "")
    if not quest_id:
        logger.warning("Planning run did not report a quest id; cursor not advanced")
        return result

    cursor.last_planned_at = datetime.now(timezone.utc).isoformat()
    cursor.last_quest_id = quest_id
    if quest_id not in cursor.pending_quest_ids:
        cursor.pending_quest_ids.append(quest_id)
    save_cursor(workspace, team_id, cursor)

    notify_controller_quest_ready(
        ouro, quest_id, agent.config.security.controller_username
    )

    goal_label = "goal " if goal else ""
    logger.info("Planning run published %squest %s", goal_label, quest_id)
    write_log(
        workspace,
        f"[planning:created] [plan](asset:{quest_id}) {goal_label}plan created",
        doc_store=_plan_doc_store(agent, team_id),
        agent_name=agent_cfg.name,
    )
    return result


# ---------------------------------------------------------------------------
# Reviewable quest discovery (CLI / force helpers)
# ---------------------------------------------------------------------------


def find_reviewable_quests(
    agent: "OuroAgent", limit: int = 10
) -> list[dict[str, Any]]:
    """The agent's own draft/open quests, drafts first (for CLI/TUI listings)."""
    ouro = agent._get_ouro_client()
    reviewable: list[dict[str, Any]] = []
    for asset in search_own_quests(agent, limit=25):
        quest_id = str(asset.get("id") or "")
        if not quest_id:
            continue
        try:
            quest = ouro.quests.retrieve(quest_id)
        except Exception:
            continue
        status = quest_status(quest)
        if status not in ("draft", "open"):
            continue
        items = quest_items(quest)
        done = sum(1 for i in items if i.get("status") in ("done", "skipped"))
        reviewable.append(
            {
                "id": quest_id,
                "name": str(read_field(quest, "name") or asset.get("name") or ""),
                "status": status,
                "team_id": str(read_field(quest, "team_id") or ""),
                "items_total": len(items),
                "items_resolved": done,
            }
        )
        if len(reviewable) >= limit:
            break
    reviewable.sort(key=lambda q: (q["status"] != "draft",))
    return reviewable
