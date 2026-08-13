"""Heartbeat mode: scheduler, active hours, and orchestration.

The heartbeat is the agent's autonomous tick. Each tick:

1. Loads the **work inbox** — actionable items across quests assigned to the
   agent plus open quests it owns. Waiting items never enter the inbox.
2. Runs planning bookkeeping: auto-approves draft plan quests whose review
   window elapsed, and — when the inbox has drained and the planning cadence
   is due — publishes a new draft plan quest.
3. Works the top of the inbox, or falls back to the general playbook.

A plan is just a quest; there is no separate plan-execution path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import HeartbeatConfig
from ..constants import parse_interval_seconds, parse_json_from_llm
from ..memory.focus import FOCUS_MEMORY_QUERIES, build_focus_memory_context
from ..tool_preloads import HEARTBEAT_INBOX, HEARTBEAT_NOTIFICATIONS, merge_preloads
from .framing import heartbeat_framing_for_kind

if TYPE_CHECKING:
    from ..agent import OuroAgent

logger = logging.getLogger(__name__)


class TickKind(str, Enum):
    """Deterministic heartbeat flavor chosen before any LLM call.

    Planning routes to a separate PLAN-mode run and never becomes a tick kind.
    """

    QUEST_WORK = "quest_work"
    OPEN_ENDED = "open_ended"
    CURIOSITY = "curiosity"


# ---------------------------------------------------------------------------
# Active hours
# ---------------------------------------------------------------------------


def is_within_active_hours(config: HeartbeatConfig) -> bool:
    """Check if the current time falls within the configured active hours.

    Returns True if no active_hours are configured (always active).
    """
    if not config.active_hours:
        return True

    start_str = config.active_hours.get("start")
    end_str = config.active_hours.get("end")
    tz_str = config.active_hours.get("timezone")

    if not start_str or not end_str:
        return True

    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(tz_str) if tz_str else None
    except (ImportError, KeyError):
        logger.warning("Invalid timezone %s, treating as always active", tz_str)
        return True

    now = datetime.now(tz) if tz else datetime.now().astimezone()
    start = datetime.strptime(start_str, "%H:%M").time()
    end = datetime.strptime(end_str, "%H:%M").time()

    current_time = now.time()

    if start <= end:
        return start <= current_time <= end
    # Wraps midnight (e.g. 22:00 - 06:00)
    return current_time >= start or current_time <= end


def estimate_beats_per_period(config: HeartbeatConfig) -> str:
    if (
        not config.active_hours
        or "start" not in config.active_hours
        or "end" not in config.active_hours
    ):
        return "continuous"

    try:
        start = datetime.strptime(config.active_hours["start"], "%H:%M").time()
        end = datetime.strptime(config.active_hours["end"], "%H:%M").time()

        start_secs = start.hour * 3600 + start.minute * 60
        end_secs = end.hour * 3600 + end.minute * 60

        if end_secs < start_secs:
            duration_secs = (24 * 3600 - start_secs) + end_secs
        else:
            duration_secs = end_secs - start_secs

        interval_secs = parse_interval_seconds(config.every)
        if not interval_secs:
            return "unknown"

        beats = max(1, int(duration_secs / interval_secs) + 1)
        return f"~{beats} beats/period"
    except Exception:
        return "unknown"


def heartbeat_interval_seconds(config: HeartbeatConfig) -> int | None:
    """Parse the configured heartbeat interval into seconds."""
    return parse_interval_seconds(config.every)


def has_future_heartbeat_in_active_window(
    config: HeartbeatConfig,
    now: Optional[datetime] = None,
) -> bool:
    """Return True when another scheduled heartbeat still fits in this active window."""
    interval_secs = heartbeat_interval_seconds(config)
    if interval_secs is None or not config.active_hours:
        return True

    start_str = config.active_hours.get("start")
    end_str = config.active_hours.get("end")
    tz_str = config.active_hours.get("timezone")
    if not start_str or not end_str:
        return True

    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(tz_str) if tz_str else None
    except (ImportError, KeyError):
        logger.warning("Invalid timezone %s, assuming future heartbeats remain", tz_str)
        return True

    current = now or datetime.now(timezone.utc)
    current = current.astimezone(tz) if tz else current.astimezone()

    start = datetime.strptime(start_str, "%H:%M").time()
    end = datetime.strptime(end_str, "%H:%M").time()
    end_dt = datetime.combine(current.date(), end, tzinfo=current.tzinfo)

    if start > end and current.time() >= start:
        end_dt = end_dt + timedelta(days=1)

    remaining_secs = (end_dt - current).total_seconds()
    return remaining_secs >= interval_secs


def is_curiosity_window(
    config: HeartbeatConfig,
    now: Optional[datetime] = None,
) -> bool:
    """Return True when the current tick falls in the wind-down curiosity window.

    The window covers the final ``curiosity.last_beats`` beats of the active
    window: with hourly beats ending at 22:00 and ``last_beats=3``, the ticks
    at 20:00, 21:00, and 22:00 are curiosity ticks. Ticks outside active hours
    are never curiosity ticks.
    """
    curiosity = getattr(config, "curiosity", None)
    if curiosity is None or not getattr(curiosity, "enabled", False):
        return False
    if not config.active_hours:
        return False

    start_str = config.active_hours.get("start")
    end_str = config.active_hours.get("end")
    tz_str = config.active_hours.get("timezone")
    if not start_str or not end_str:
        return False

    interval_secs = heartbeat_interval_seconds(config)
    if not interval_secs:
        return False

    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(tz_str) if tz_str else None
    except (ImportError, KeyError):
        logger.warning("Invalid timezone %s, skipping curiosity window", tz_str)
        return False

    current = now or datetime.now(timezone.utc)
    current = current.astimezone(tz) if tz else current.astimezone()

    start = datetime.strptime(start_str, "%H:%M").time()
    end = datetime.strptime(end_str, "%H:%M").time()

    current_time = current.time()
    if start <= end:
        if not (start <= current_time <= end):
            return False
    elif not (current_time >= start or current_time <= end):
        return False

    end_dt = datetime.combine(current.date(), end, tzinfo=current.tzinfo)
    if start > end and current.time() >= start:
        end_dt = end_dt + timedelta(days=1)

    remaining_secs = (end_dt - current).total_seconds()
    last_beats = max(1, int(getattr(curiosity, "last_beats", 3)))
    return 0 <= remaining_secs < last_beats * interval_secs


def format_active_period_status(config: HeartbeatConfig) -> str:
    """One-line summary for logging: configured window (if any) and whether now is inside it."""
    if not config.active_hours:
        return "active_period=always"

    start_str = config.active_hours.get("start")
    end_str = config.active_hours.get("end")
    tz_label = config.active_hours.get("timezone") or "local"

    if not start_str or not end_str:
        return "active_period=always (active_hours missing start/end)"

    in_window = is_within_active_hours(config)
    state = "active" if in_window else "inactive"
    beats_est = estimate_beats_per_period(config)
    return f"period={start_str}–{end_str} ({tz_label}); now={state}; {beats_est}"


# ---------------------------------------------------------------------------
# Playbook / direction context loading
# ---------------------------------------------------------------------------


def _load_playbook(
    agent: "OuroAgent", heartbeat_doc_store, doc_name: str = "HEARTBEAT"
) -> str | None:
    """Load a playbook doc: team doc store → global doc store → local file."""
    playbook = None
    if heartbeat_doc_store:
        playbook = (
            heartbeat_doc_store.read(f"{doc_name}:{agent.config.agent.name}") or None
        )
    if not playbook and heartbeat_doc_store is not agent.doc_store and agent.doc_store:
        playbook = agent.doc_store.read(f"{doc_name}:{agent.config.agent.name}") or None
    if not playbook:
        playbook_path = agent.config.agent.workspace / f"{doc_name}.md"
        if playbook_path.exists():
            playbook = playbook_path.read_text()
    return playbook


def _load_work_direction_context(agent: "OuroAgent", team_id: str | None) -> str:
    """Load durable work-direction guidance that should constrain heartbeats.

    When a team is known, recall global + that team once. When no team is
    selected, recall only the highest-signal global guidance — never fan out
    across every team (that path previously triggered hundreds of embeds).
    """
    agent_cfg = getattr(getattr(agent, "config", None), "agent", None)
    agent_name = getattr(agent_cfg, "name", "")
    if not agent_name:
        return ""

    guidance = (
        "Treat these as strong controller/user direction for choosing heartbeat "
        "focus. If this conflicts with stale tasks, plans, or broad research "
        "interests, follow the work direction and avoid the conflicting work."
    )
    memory = getattr(agent, "memory", None)
    contexts: list[str] = []

    # Always include a bounded global pass.
    global_context = build_focus_memory_context(
        memory,
        agent_name,
        heading="Current Work Direction Guidance",
        guidance=guidance,
        limit=6,
        reinforce=False,
    )
    if global_context:
        contexts.append(global_context)

    if team_id:
        team_context = build_focus_memory_context(
            memory,
            agent_name,
            team_id=team_id,
            heading=f"Current Work Direction Guidance for team {team_id}",
            guidance=guidance,
            limit=6,
            reinforce=False,
        )
        if team_context:
            contexts.append(team_context)

    # Deduplicate identical bullet lines across global/team blocks.
    if not contexts:
        return ""
    seen_lines: set[str] = set()
    merged: list[str] = []
    for block in contexts:
        for line in block.splitlines():
            key = line.strip()
            if key.startswith("- ") and key in seen_lines:
                continue
            if key.startswith("- "):
                seen_lines.add(key)
            merged.append(line)
    return "\n".join(merged)


def build_recent_tick_digest(
    agent: "OuroAgent",
    *,
    team_id: str | None = None,
    limit: int = 5,
) -> str:
    """Compact digest of recent heartbeat and controller-decision outcomes.

    Lets the heartbeat see what recent ticks already did — so it avoids
    repeating a slice and can audit priority tiers against fresh evidence
    instead of re-deriving it from live reads. Best-effort: any failure (no
    run log, query error) yields an empty string.
    """
    run_log = getattr(agent, "_run_log", None)
    query_runs = getattr(run_log, "query_runs", None)
    if not callable(query_runs):
        return ""

    base_kwargs: dict[str, Any] = {"limit": limit}
    if team_id:
        base_kwargs["team_id"] = team_id
    try:
        heartbeat_rows = query_runs(mode="heartbeat", **base_kwargs)
        decision_rows = query_runs(event_type="controller-decision", **base_kwargs)
    except Exception as e:
        logger.debug("Failed to query recent heartbeat outcomes: %s", e)
        return ""

    rows = list(heartbeat_rows or []) + list(decision_rows or [])
    if not rows:
        return ""
    rows.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
    rows = rows[:limit]

    lines: list[str] = []
    for row in rows:
        started = str(row.get("started_at") or "")[:16].replace("T", " ")
        status = str(row.get("status") or "")
        event_type = str(row.get("event_type") or "")
        if event_type == "controller-decision":
            tier = "controller-decision"
            summary = _summarize_tick_result(row.get("result"))
            if not summary:
                summary = _summarize_tick_result(row.get("task"))
        else:
            intent = str(row.get("preflight_intent") or "")
            complexity = str(row.get("preflight_complexity") or "")
            tier = intent if intent in ("pass", "act") else "?"
            if complexity.startswith("priority:"):
                tier = f"{tier} {complexity}"
            summary = _summarize_tick_result(row.get("result"))
        parts = [p for p in (started, status, tier) if p]
        line = "- " + " · ".join(parts)
        if summary:
            line += f" — {summary}"
        lines.append(line)

    if not lines:
        return ""
    return (
        "## Recent Heartbeat Outcomes\n"
        "What the last few ticks already did (most recent first), including "
        "controller-decision resumes. Do not repeat a slice that is clearly "
        "finished; use this as fresh evidence when auditing priority tiers.\n"
        + "\n".join(lines)
    )


def _summarize_tick_result(result: Any, *, max_len: int = 120) -> str:
    """One-line summary of a heartbeat run's stored result JSON/text."""
    text = str(result or "").strip()
    if not text:
        return ""
    parsed = parse_json_from_llm(text)
    if isinstance(parsed, dict):
        action = str(parsed.get("action") or "").strip()
        details = str(parsed.get("details") or parsed.get("summary") or "").strip()
        combined = " ".join(p for p in (action, details) if p) or text
    else:
        combined = text
    combined = " ".join(combined.split())
    if len(combined) > max_len:
        combined = combined[: max_len - 1].rstrip() + "…"
    return combined


def _sorted_team_ids(agent: "OuroAgent") -> list[str]:
    team_registry = getattr(agent, "team_registry", None)
    if not team_registry:
        return []
    return sorted(team_registry.team_ids())


def _parse_signal_time(value: Any) -> float:
    if not value:
        return 0.0
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _planning_team_is_writable(agent: "OuroAgent", team_id: str) -> bool:
    registry = getattr(agent, "team_registry", None)
    get_team = getattr(registry, "get_team", None)
    if not callable(get_team):
        return True
    team = get_team(team_id)
    return bool(getattr(team, "agent_can_create", True)) if team else True


def _team_label_fragments(agent: "OuroAgent", team_id: str) -> set[str]:
    fragments = {team_id.lower()}
    registry = getattr(agent, "team_registry", None)
    get_team = getattr(registry, "get_team", None)
    if callable(get_team):
        team = get_team(team_id)
        if team:
            for attr in ("name", "slug"):
                value = str(getattr(team, attr, "") or "").strip().lower()
                if value:
                    fragments.add(value)
    return fragments


def _text_mentions_team_label(text: str, labels: set[str]) -> bool:
    lowered = text.lower()
    for label in labels:
        if not label:
            continue
        if re.search(rf"(?<![\w-]){re.escape(label)}(?![\w-])", lowered):
            return True
    return False


def _team_direction_score(agent: "OuroAgent", team_id: str) -> tuple[float, float, str]:
    agent_cfg = getattr(getattr(agent, "config", None), "agent", None)
    agent_name = getattr(agent_cfg, "name", "")
    memory = getattr(agent, "memory", None)
    if not agent_name or not memory:
        return 0.0, 0.0, ""

    best_score = 0.0
    best_time = 0.0
    best_reason = ""

    def consider(matches: list[Any], base: float, reason: str) -> None:
        nonlocal best_score, best_time, best_reason
        for match in matches:
            text = (getattr(match, "text", "") or "").strip()
            if not text:
                continue
            strength = float(getattr(match, "strength", 0.8) or 0.8)
            signal_time = _parse_signal_time(getattr(match, "created_at", ""))
            score = base + min(10.0, max(0.0, strength * 10.0))
            if score > best_score or (score == best_score and signal_time > best_time):
                best_score = score
                best_time = signal_time
                best_reason = reason

    for query in FOCUS_MEMORY_QUERIES:
        try:
            team_matches = memory.search(
                query=query,
                agent_id=agent_name,
                team_id=team_id,
                scope="team",
                category="direction",
                limit=4,
            )
            consider(list(team_matches or []), 100.0, "team direction memory")
        except TypeError:
            try:
                team_matches = memory.search(
                    query=query,
                    agent_id=agent_name,
                    team_id=team_id,
                    scope="team",
                    limit=4,
                )
                consider(list(team_matches or []), 95.0, "team focus memory")
            except Exception as e:
                logger.debug("Failed to score team direction memory: %s", e)
                break
        except Exception as e:
            logger.debug("Failed to score team direction memory: %s", e)
            break

    labels = _team_label_fragments(agent, team_id)
    for query in FOCUS_MEMORY_QUERIES:
        try:
            global_matches = memory.search(
                query=query,
                agent_id=agent_name,
                scope="global",
                category="direction",
                limit=6,
            )
        except TypeError:
            try:
                global_matches = memory.search(
                    query=query,
                    agent_id=agent_name,
                    scope="global",
                    limit=6,
                )
            except Exception as e:
                logger.debug("Failed to score global direction memory: %s", e)
                break
        except Exception as e:
            logger.debug("Failed to score global direction memory: %s", e)
            break
        matching_global = [
            match
            for match in list(global_matches or [])
            if _text_mentions_team_label(getattr(match, "text", "") or "", labels)
        ]
        consider(matching_global, 85.0, "global direction memory naming team")

    return best_score, best_time, best_reason


def _recent_run_team_scores(
    agent: "OuroAgent",
    candidate_team_ids: set[str],
    *,
    lookback_days: int = 7,
) -> dict[str, tuple[float, float, str]]:
    run_log = getattr(agent, "_run_log", None)
    query_runs = getattr(run_log, "query_runs", None)
    if not callable(query_runs):
        return {}

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    try:
        rows = query_runs(since=since, limit=80)
    except Exception as e:
        logger.debug("Failed to query recent run history for team selection: %s", e)
        return {}

    security = getattr(getattr(agent, "config", None), "security", None)
    controller_ids = set(getattr(security, "resolved_controller_ids", []) or [])
    trusted_ids = set(getattr(security, "resolved_trusted_ids", []) or [])
    scores: dict[str, tuple[float, float, str]] = {}

    for index, row in enumerate(rows or []):
        team_id = str(row.get("team_id") or "")
        if team_id not in candidate_team_ids:
            continue
        user_id = str(row.get("user_id") or "")
        role = str(row.get("capability_role") or "").lower()
        trusted_actor = (
            role in {"controller", "trusted"}
            or user_id in controller_ids
            or user_id in trusted_ids
        )
        base = 60.0 if trusted_actor else 30.0
        score = max(1.0, base - float(index))
        signal_time = _parse_signal_time(row.get("started_at"))
        reason = (
            "recent controller/trusted activity"
            if trusted_actor
            else "recent agent activity"
        )
        current = scores.get(team_id)
        if (
            current is None
            or score > current[0]
            or (score == current[0] and signal_time > current[1])
        ):
            scores[team_id] = (score, signal_time, reason)
    return scores


def _select_planning_team_id(
    agent: "OuroAgent", candidate_team_ids: list[str]
) -> str | None:
    """Pick the planning team by direction-memory and recent-activity signals.

    Falls back to the first candidate (sorted order) when nothing scores.
    """
    if not candidate_team_ids:
        return None
    if len(candidate_team_ids) == 1:
        return candidate_team_ids[0]

    scored: dict[str, tuple[float, float, list[str]]] = {
        team_id: (0.0, 0.0, []) for team_id in candidate_team_ids
    }

    for team_id in candidate_team_ids:
        score, signal_time, reason = _team_direction_score(agent, team_id)
        if score:
            total, latest, reasons = scored[team_id]
            scored[team_id] = (
                total + score,
                max(latest, signal_time),
                reasons + [reason],
            )

    for team_id, (score, signal_time, reason) in _recent_run_team_scores(
        agent, set(candidate_team_ids)
    ).items():
        total, latest, reasons = scored[team_id]
        scored[team_id] = (
            total + score,
            max(latest, signal_time),
            reasons + [reason],
        )

    ranked = [
        (score, signal_time, team_id, reasons)
        for team_id, (score, signal_time, reasons) in scored.items()
        if score > 0
    ]
    if not ranked:
        return sorted(candidate_team_ids)[0]
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    selected_score, _signal_time, selected, reasons = ranked[0]
    logger.info(
        "Selected planning team %s by policy score=%.1f reasons=%s",
        selected[:8],
        selected_score,
        ", ".join(reasons) or "none",
    )
    return selected


# ---------------------------------------------------------------------------
# Work inbox — actionable quest items across assigned + owned quests
# ---------------------------------------------------------------------------


def _advance_due_recurring_items(
    agent: "OuroAgent",
    items: list[dict[str, Any]],
    now: datetime | None = None,
) -> None:
    """Re-park due recurring waiting items so they poll on a cadence.

    When a recurring check (``waiting_check_every`` set) has come due it is
    handed to this heartbeat once; advancing ``waiting_until`` by the interval
    keeps it out of the inbox until the next tick so it doesn't consume every
    heartbeat. Completing the item stops the recurrence. Failures are non-fatal:
    the item is still worked this heartbeat, just not rescheduled.
    """
    from .planning import item_is_waiting

    now = now or datetime.now(timezone.utc)
    client = None
    for item in items:
        every = item.get("waiting_check_every")
        if not every:
            continue
        if item.get("status") not in ("pending", "in_progress"):
            continue
        if item_is_waiting(item, now):
            continue
        seconds = parse_interval_seconds(str(every))
        if not seconds:
            continue
        quest_id = item.get("quest_id")
        item_id = item.get("id")
        if not quest_id or not item_id:
            continue
        next_iso = (now + timedelta(seconds=seconds)).isoformat()
        try:
            client = client or agent._get_ouro_client()
            client.quests.update_item(
                str(quest_id), str(item_id), waiting_until=next_iso
            )
        except Exception as e:
            logger.warning(
                "Failed to reschedule recurring waiting item %s: %s",
                str(item_id)[:8],
                e,
            )
            continue
        item["waiting_until"] = next_iso


def load_assigned_quest_items(
    agent: "OuroAgent", limit: int = 10
) -> list[dict[str, Any]]:
    """Fetch actionable quest items assigned to this agent, if supported by the API."""
    from .planning import item_is_waiting, normalize_item

    if not getattr(agent, "own_user_id", None):
        return []
    try:
        ouro = agent._get_ouro_client()
        list_assigned = getattr(ouro.quests, "list_assigned_items", None)
        if not list_assigned:
            logger.debug("Assigned quest item listing is not available in this SDK")
            return []
        raw = list_assigned(limit=limit, status=["pending", "in_progress"])
        if isinstance(raw, dict):
            raw = raw.get("data") or []
        if not isinstance(raw, list):
            return []
        items = []
        for item in raw:
            if not isinstance(item, dict) or item_is_waiting(item):
                continue
            normalized = normalize_item(item)
            normalized["inbox_source"] = "assigned"
            items.append(normalized)
        return items
    except Exception as e:
        logger.warning("Failed to load assigned quest items: %s", e)
        return []


# Back-compat alias for older call sites / tests.
_load_assigned_quest_items = load_assigned_quest_items


def _quest_asset_summary(quest: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    from ..syncing import read_field

    quest_details = read_field(quest, "quest")
    return {
        "id": str(read_field(quest, "id") or fallback.get("id") or ""),
        "name": read_field(quest, "name") or fallback.get("name") or "Untitled quest",
        "org_id": str(read_field(quest, "org_id") or fallback.get("org_id") or ""),
        "team_id": str(read_field(quest, "team_id") or fallback.get("team_id") or ""),
        "user_id": str(read_field(quest, "user_id") or fallback.get("user_id") or ""),
        "quest": {
            "status": read_field(quest_details, "status"),
            "type": read_field(quest_details, "type"),
        },
    }


def _load_owned_open_quest_items(
    agent: "OuroAgent",
    *,
    quest_limit: int = 25,
    item_limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch actionable items from open quests owned by this agent.

    Plan quests land here like any other owned quest — planning publishes a
    quest and execution flows through this inbox. Draft quests are excluded
    (they are awaiting approval), as are waiting items.
    """
    from .planning import (
        item_is_waiting,
        quest_items,
        quest_status,
        search_own_quests,
    )

    own_user_id = getattr(agent, "own_user_id", None)
    if not own_user_id:
        return []
    try:
        ouro = agent._get_ouro_client()
        retrieve = getattr(getattr(ouro, "quests", None), "retrieve", None)
        if not retrieve:
            logger.debug("Owned quest discovery is not available in this SDK")
            return []

        actionable: list[dict[str, Any]] = []
        for asset in search_own_quests(agent, limit=quest_limit):
            quest_id = str(asset.get("id") or "")
            if not quest_id:
                continue
            try:
                quest = retrieve(quest_id)
            except Exception as e:
                logger.debug("Failed to retrieve owned quest %s: %s", quest_id[:8], e)
                continue

            if quest_status(quest) != "open":
                continue

            quest_asset = _quest_asset_summary(quest, asset)
            for item in quest_items(quest):
                if item.get("status") not in ("pending", "in_progress"):
                    continue
                if item_is_waiting(item):
                    continue
                item["quest_asset"] = quest_asset
                item["inbox_source"] = "owned"
                actionable.append(item)
                if len(actionable) >= item_limit:
                    return actionable
        return actionable
    except Exception as e:
        logger.warning("Failed to load owned open quest items: %s", e)
        return []


def load_work_inbox(agent: "OuroAgent", limit: int = 10) -> list[dict[str, Any]]:
    """The ranked, actionable quest-item inbox for one heartbeat tick.

    Items assigned by others come first (strongest signal), then items from
    the agent's own open quests, newest quest activity first. Waiting items
    never appear; recurring checks appear only when due.
    """
    inbox = load_assigned_quest_items(agent, limit=limit)
    seen = {(item.get("quest_id"), item.get("id")) for item in inbox}
    for item in _load_owned_open_quest_items(agent, item_limit=limit):
        key = (item.get("quest_id"), item.get("id"))
        if key in seen:
            continue
        seen.add(key)
        inbox.append(item)
    return inbox[:limit]


def count_plans_created_since(
    agent: "OuroAgent",
    *,
    since: datetime,
    team_ids: list[str] | None = None,
) -> int:
    """How many plan quests this agent published on or after *since*.

    Prefers per-team planning cursors (``last_planned_at`` / ``last_quest_id``)
    so skips that advance the cursor still count toward the daily budget.
    Falls back to own-quest created_at when cursors are empty.
    """
    from .planning import load_cursor, search_own_quests

    workspace = agent.config.agent.workspace
    counted_ids: set[str] = set()
    count = 0
    for team_id in team_ids or _sorted_team_ids(agent):
        cursor = load_cursor(workspace, team_id)
        last_at = None
        if cursor.last_planned_at:
            try:
                text = cursor.last_planned_at
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                last_at = datetime.fromisoformat(text)
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
            except ValueError:
                last_at = None
        if last_at is not None and last_at >= since:
            count += 1
            if cursor.last_quest_id:
                counted_ids.add(cursor.last_quest_id)

    # Also count additional own quests created in-window that cursors missed
    # (e.g. multi-plan same day before skip path existed).
    try:
        for asset in search_own_quests(agent, limit=30):
            quest_id = str(asset.get("id") or "")
            if not quest_id or quest_id in counted_ids:
                continue
            created_raw = str(asset.get("created_at") or "")
            if not created_raw:
                continue
            try:
                text = created_raw
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                created = datetime.fromisoformat(text)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if created >= since:
                count += 1
                counted_ids.add(quest_id)
    except Exception as e:
        logger.debug("Failed to count recent plan quests: %s", e)
    return count


def count_owned_open_backlog(agent: "OuroAgent", *, quest_limit: int = 25) -> int:
    """Open owned-quest items including waiting/parked ones.

    Waiting items are excluded from the work inbox (so they don't starve
    heartbeats) but still represent real backlog that should block minting
    new plans.
    """
    from .planning import (
        item_is_open,
        quest_items,
        quest_status,
        search_own_quests,
    )

    own_user_id = getattr(agent, "own_user_id", None)
    if not own_user_id:
        return 0
    try:
        ouro = agent._get_ouro_client()
    except Exception:
        return 0
    total = 0
    try:
        for asset in search_own_quests(agent, limit=quest_limit):
            quest_id = str(asset.get("id") or "")
            if not quest_id:
                continue
            try:
                quest = ouro.quests.retrieve(quest_id)
            except Exception:
                continue
            status = quest_status(quest)
            if status != "open":
                continue
            for item in quest_items(quest):
                if item_is_open(item):
                    total += 1
    except Exception as e:
        logger.warning("Failed to count owned backlog: %s", e)
    return total


def planning_budget_blocks(
    agent: "OuroAgent",
    team_ids: list[str],
    *,
    now: datetime | None = None,
) -> str | None:
    """Return a human reason to skip planning, or None if planning may run."""
    planning_cfg = agent.config.planning
    now = now or datetime.now(timezone.utc)
    max_per_day = int(getattr(planning_cfg, "max_plans_per_day", 2) or 0)
    backlog_limit = int(getattr(planning_cfg, "backlog_limit", 8) or 0)

    if max_per_day > 0:
        since = now - timedelta(hours=24)
        created = count_plans_created_since(agent, since=since, team_ids=team_ids)
        if created >= max_per_day:
            return (
                f"plan budget exhausted ({created}/{max_per_day} plans in last 24h)"
            )

    if backlog_limit > 0:
        backlog = count_owned_open_backlog(agent)
        if backlog >= backlog_limit:
            return (
                f"open backlog too large ({backlog} items >= limit {backlog_limit}, "
                "including waiting/parked)"
            )
    return None


def _inbox_item_quest(item: dict[str, Any]) -> dict[str, Any]:
    quest = item.get("quest_asset")
    return quest if isinstance(quest, dict) else {}


def inbox_team_id(items: list[dict[str, Any]], agent: "OuroAgent") -> str | None:
    """Team scope for the tick: the top inbox item's team, when it's ours."""
    known = set(_sorted_team_ids(agent))
    for item in items:
        team_id = str(_inbox_item_quest(item).get("team_id") or "")
        if team_id and team_id in known:
            return team_id
    return None


def format_inbox_items(items: list[dict[str, Any]]) -> str:
    """Format inbox/assigned quest items for playbooks and CLI display."""
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        quest = _inbox_item_quest(item)
        quest_id = str(item.get("quest_id") or quest.get("id") or "")
        quest_name = quest.get("name") or "Untitled quest"
        status = item.get("status") or "unknown"
        item_id = str(item.get("id") or "")
        description = item.get("description") or "(no description)"
        details = []
        if item.get("inbox_source") == "assigned":
            details.append("assigned to you")
        if item.get("submission_assets"):
            import json as _json

            details.append(
                f"submission_assets={_json.dumps(item['submission_assets'])}"
            )
        if item.get("eval_route_id"):
            details.append(f"eval_route_id={item['eval_route_id']}")
        suffix = f" ({'; '.join(details)})" if details else ""
        # A due recurring/parked item carries a handoff note from the tick
        # that parked it. Without it the item reads as fresh work and the
        # agent will redo slices that are already done.
        waiting_note = ""
        if item.get("waiting_on") or item.get("waiting_check_every"):
            parts = []
            if item.get("waiting_on"):
                parts.append(f"note from when it was parked: {item['waiting_on']}")
            if item.get("waiting_check_every"):
                parts.append(
                    f"recurring check every {item['waiting_check_every']} — "
                    "verify the awaited event, act only on what's new, and "
                    "complete the item if its work is already done"
                )
            waiting_note = (
                "\n   This item resurfaced from a waiting state; "
                + "; ".join(parts)
            )
        lines.append(
            f"{idx}. Quest `{quest_id}` — {quest_name}\n"
            f"   Item `{item_id}` [{status}]: {description}{suffix}{waiting_note}"
        )
    return "\n".join(lines)


# Back-compat aliases.
_format_inbox_items = format_inbox_items
format_assigned_quest_items = format_inbox_items


def build_quest_work_playbook(items: list[dict[str, Any]]) -> str:
    """Data-shaped quest inbox for a quest_work heartbeat tick.

    Surfaces the actionable items and the signals needed to pick one (ranking,
    resurfaced handoffs, adaptive/assigned nature). Quest tool mechanics live
    in the (kind-specific) system framing, so this playbook stays out of
    tool recipes.
    """
    has_assigned = any(i.get("inbox_source") == "assigned" for i in items)
    has_owned = any(i.get("inbox_source") == "owned" for i in items)

    adaptive_guidance = ""
    if has_owned:
        adaptive_guidance = (
            "On quests you own, if finished work has made a later pending item "
            "stale or improvable, prefer revising that item over executing it "
            "as-is, and note the pivot on the quest.\n\n"
        )
    assigned_guidance = ""
    if has_assigned:
        assigned_guidance = (
            "Some items are assigned to you on quests owned by others; plan an "
            "entry submission rather than rewriting the owner's plan.\n\n"
        )
    return (
        "You are choosing and executing this heartbeat's quest work.\n\n"
        "These are the actionable items across quests assigned to you and open "
        "quests you own, in priority order. Choose ONE item — normally the "
        "first, unless a later item is clearly more urgent — that yields one "
        "meaningful slice of progress that changes platform state or produces "
        "a useful artifact. Do not try to clear the whole inbox in a single "
        "tick.\n\n"
        "If an item says it resurfaced from a waiting state, treat the parked "
        "note as the handoff from the prior tick: verify the awaited event and "
        "act only on what is still unfinished. Do not redo finished slices "
        "(sends already logged, prospects already seeded, drafts already "
        "posted) just because the description still lists them.\n\n"
        f"{adaptive_guidance}"
        f"{assigned_guidance}"
        "## Quest Inbox\n"
        f"{format_inbox_items(items)}\n\n"
        "Quest mechanics (marking an item in progress, completing vs. "
        "submitting entries, parking blocked items with waiting fields, and "
        "closing out a finished quest) are in your MODE framing. Use the exact "
        "quest/item/asset IDs and done-conditions from the inbox."
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def start_scheduler(agent, config: HeartbeatConfig):
    scheduler = AsyncIOScheduler()

    match = re.match(r"(\d+)([smhd])", config.every)
    if not match:
        logger.error("Invalid heartbeat interval: %s", config.every)
        return

    val = int(match.group(1))
    unit = match.group(2)

    start_hour = 0
    start_minute = 0
    if config.active_hours and "start" in config.active_hours:
        try:
            start_time = datetime.strptime(config.active_hours["start"], "%H:%M").time()
            start_hour = start_time.hour
            start_minute = start_time.minute
        except Exception:
            pass

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if unit == "d":
        trigger = CronTrigger(day=f"*/{val}", hour=start_hour, minute=start_minute)
    else:
        kwargs = {
            "s": {"seconds": val},
            "m": {"minutes": val},
            "h": {"hours": val},
        }[unit]

        tz = None
        if config.active_hours and "timezone" in config.active_hours:
            try:
                import zoneinfo

                tz = zoneinfo.ZoneInfo(config.active_hours["timezone"])
            except Exception:
                pass

        # Anchor date in the past to align intervals to the start time
        anchor = datetime(2024, 1, 1, start_hour, start_minute, tzinfo=tz)
        trigger = IntervalTrigger(**kwargs, start_date=anchor)

    async def _run_heartbeat():
        active = is_within_active_hours(config)
        if not active:
            logger.info("Outside active hours, skipping heartbeat")
            # Don't log next_run_time here, since the next trigger will also be skipped
            # until we actually enter active hours.
            return

        try:
            logger.info("Running heartbeat...")
            import ouro_agents.server as server_module

            server_module.last_heartbeat = datetime.now(timezone.utc)

            await agent.heartbeat()
            if job and hasattr(job, "next_run_time") and job.next_run_time:
                logger.info(
                    "Next heartbeat scheduled for: %s",
                    job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
        except Exception as e:
            logger.error("Heartbeat failed: %s", e)

    job = scheduler.add_job(
        _run_heartbeat,
        trigger,
        next_run_time=trigger.get_next_fire_time(None, datetime.now(timezone.utc)),
    )
    scheduler.start()

    next_run = job.next_run_time if hasattr(job, "next_run_time") else None
    next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else "unknown"
    logger.info(
        "Started heartbeat scheduler: every %s; %s; next_run=%s",
        config.every,
        format_active_period_status(config),
        next_run_str,
    )


# ---------------------------------------------------------------------------
# Heartbeat orchestration
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatTaskContext:
    """Playbook/task payload assembled for a heartbeat run or dry-run."""

    playbook: str | None
    team_id: str | None
    source: str
    tick_kind: TickKind = TickKind.OPEN_ENDED
    framing_override: str = ""
    include_plans_index: bool = True
    preload_tools: list[str] = field(default_factory=list)
    inbox: list[dict[str, Any]] = field(default_factory=list)


def resolve_heartbeat_model(agent: "OuroAgent"):
    """Build the model used for heartbeat / force CLI entry points."""
    hb_model_id = (
        (agent._model_id_for_role("heartbeat") if hasattr(agent, "_model_id_for_role") else None)
        or agent.config.heartbeat.model
        or agent.config.agent.model
    )
    return agent._build_model(hb_model_id, heartbeat=True, role="heartbeat")


def refresh_heartbeat_platform_context(agent: "OuroAgent") -> None:
    """Refresh cached platform context; log and continue on failure."""
    try:
        agent._refresh_platform_context()
    except Exception as e:
        logger.warning("Failed to refresh platform context during heartbeat: %s", e)


def heartbeat_servers(agent: "OuroAgent") -> list[str]:
    """Default MCP servers for heartbeat / planning / review runs."""
    return list(getattr(agent.config.heartbeat, "servers", None) or ["ouro"])


def _append_shared_context_snapshot(
    agent: "OuroAgent",
    playbook: str,
    *,
    tick_kind: TickKind,
    team_id: str | None,
) -> str:
    """Append the per-kind memory/task index the heartbeat can read_context."""
    from ..memory.context_loader import (
        build_cross_team_task_index,
        build_memory_index,
    )

    if tick_kind == TickKind.QUEST_WORK and team_id:
        task_index = build_memory_index(
            agent.config.agent.workspace, team_id=team_id
        )
    elif tick_kind in (TickKind.OPEN_ENDED, TickKind.CURIOSITY):
        labels: dict[str, str] = {}
        registry = getattr(agent, "team_registry", None)
        if registry is not None:
            get_team = getattr(registry, "get_team", None)
            for tid in sorted(registry.team_ids()):
                team = get_team(tid) if callable(get_team) else None
                labels[tid] = (
                    getattr(team, "slug", None)
                    or getattr(team, "name", None)
                    or tid[:8]
                )
        task_index = build_cross_team_task_index(
            agent.config.agent.workspace, team_labels=labels
        )
    else:
        # Quest work without a known team: skip the cross-team fan-out.
        task_index = ""

    if task_index:
        playbook = f"{playbook}\n\n## Shared Context Snapshot\n{task_index}"
    return playbook


def _append_notification_inbox(
    agent: "OuroAgent",
    playbook: str | None,
    preload_tools: list[str],
) -> tuple[str | None, list[str]]:
    """Append the Notification Inbox section and extend preload tools.

    Failures are swallowed so a broken notifications API never aborts the tick.
    An inbox-only playbook is valid when there is no other work.
    """
    try:
        from ..notification_inbox import build_notification_inbox

        get_client = getattr(agent, "_get_ouro_client", None)
        if not callable(get_client):
            return playbook, preload_tools
        ouro = get_client()
        if ouro is None:
            return playbook, preload_tools

        inbox_cfg = agent.config.event_delivery.notification_inbox
        notif_inbox = build_notification_inbox(ouro, inbox_cfg)
        if not notif_inbox.section:
            return playbook, preload_tools

        if playbook:
            playbook = f"{playbook}\n\n{notif_inbox.section}"
        else:
            playbook = notif_inbox.section

        preload_tools = merge_preloads(preload_tools, HEARTBEAT_NOTIFICATIONS)
    except Exception:
        logger.warning("Failed to attach notification inbox to heartbeat", exc_info=True)

    return playbook, preload_tools


def _build_curiosity_playbook(playbook: str, inbox: list[dict[str, Any]]) -> str:
    """Compose the curiosity tick playbook with an urgency-only inbox check."""
    if not inbox:
        return playbook
    lines = []
    for item in inbox[:3]:
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"- {description}")
    section = (
        "## Urgency check (then set work aside)\n"
        f"The quest inbox still has {len(inbox)} actionable item(s). This is "
        "your curiosity window: break away only for something genuinely urgent "
        "— a person waiting on your reply right now, or something you shipped "
        "actively breaking. Everything else keeps until tomorrow; do not work "
        "the inbox tonight."
    )
    if lines:
        section += "\n\nTop of the inbox, for the urgency check only:\n" + "\n".join(lines)
    return f"{playbook}\n\n{section}"


def build_heartbeat_task_context(
    agent: "OuroAgent",
    *,
    inbox: list[dict[str, Any]] | None = None,
    advance_recurring: bool = True,
) -> HeartbeatTaskContext:
    """Assemble the heartbeat playbook/task (shared by tick + dry-run).

    Does not run planning bookkeeping. When *inbox* is omitted, loads it.
    Set *advance_recurring* False for read-only dry-runs.

    Tick kind is deterministic: a non-empty inbox → quest_work; otherwise
    open_ended. Planning never reaches this function (it routes earlier).
    """
    items = list(inbox) if inbox is not None else load_work_inbox(agent)
    team_id: str | None = None
    doc_store = agent.doc_store
    playbook = None
    source = "none"
    preload_tools: list[str] = []
    tick_kind = TickKind.OPEN_ENDED

    # Curiosity window: the wind-down beats at the end of the active day are
    # reserved for self-directed exploration. The quest inbox is set aside
    # (surfaced only as an urgency check) and CURIOSITY.md drives the tick.
    if is_curiosity_window(agent.config.heartbeat):
        curiosity_playbook = _load_playbook(agent, doc_store, doc_name="CURIOSITY")
        if curiosity_playbook:
            tick_kind = TickKind.CURIOSITY
            playbook = _build_curiosity_playbook(curiosity_playbook, items)
            source = "curiosity"
        else:
            logger.info(
                "Curiosity window active but no CURIOSITY playbook found; "
                "falling back to normal heartbeat behavior"
            )

    if playbook is None and items:
        tick_kind = TickKind.QUEST_WORK
        if advance_recurring:
            _advance_due_recurring_items(agent, items)
        team_id = inbox_team_id(items, agent)
        if team_id:
            doc_store = agent.doc_store_for(team_id)
        playbook = build_quest_work_playbook(items)
        source = "quest-inbox"
        preload_tools = list(HEARTBEAT_INBOX)
        # Compose with the general agent heartbeat policy: inbox chooses the
        # work item; policy supplies prioritization, constraints, and closeout.
        policy = _load_playbook(agent, doc_store)
        if policy:
            playbook = (
                f"{playbook}\n\n## Heartbeat Policy\n"
                "The quest inbox above is the work in front of you. Apply the "
                "policy below for prioritization, safety constraints, and "
                "closeout — do not ignore an ordered priority ladder just because "
                "a quest item is present.\n\n"
                f"{policy}"
            )

    if not playbook:
        playbook = _load_playbook(agent, doc_store)
        if playbook:
            source = "playbook"
            tick_kind = TickKind.OPEN_ENDED

    if not playbook:
        # Notification inbox alone can justify a tick when there is no other work.
        playbook, preload_tools = _append_notification_inbox(
            agent, playbook, preload_tools
        )
        if playbook:
            source = "notification-inbox"
            tick_kind = TickKind.OPEN_ENDED
        return HeartbeatTaskContext(
            playbook=playbook,
            team_id=team_id,
            source=source,
            tick_kind=tick_kind,
            framing_override=heartbeat_framing_for_kind(tick_kind.value),
            include_plans_index=tick_kind == TickKind.OPEN_ENDED,
            preload_tools=preload_tools,
            inbox=items,
        )

    from ..memory.dream import dream_health_note

    health_note = dream_health_note(agent.config.agent.workspace)
    if health_note:
        playbook = f"{playbook}\n\n## Memory Maintenance Health\n{health_note}"

    # Work-direction recall steers focus toward current priorities; curiosity
    # ticks are deliberately self-directed, so it would defeat their purpose.
    direction_context = ""
    if tick_kind != TickKind.CURIOSITY:
        direction_context = _load_work_direction_context(agent, team_id)
    if direction_context:
        playbook = (
            f"{playbook}\n\n## Current Work Direction\n{direction_context}\n\n"
            "Before choosing work for this heartbeat, apply the current work "
            "direction above as a hard priority signal. Do not choose unrelated "
            "research or browsing when a current direction names a concrete focus."
        )

    standing = ""
    controller_manager = getattr(agent, "_controller_questions", None)
    standing_fn = getattr(controller_manager, "standing_decisions_context", None)
    if callable(standing_fn):
        try:
            standing = standing_fn() or ""
        except Exception:
            logger.debug("Failed to load standing controller decisions", exc_info=True)
    if standing:
        playbook = (
            f"{playbook}\n\n{standing}\n\n"
            "Standing controller decisions bind this tick. Do not re-ask them or "
            "take an action they forbid."
        )

    playbook = _append_shared_context_snapshot(
        agent, playbook, tick_kind=tick_kind, team_id=team_id
    )

    # Recent outcomes: team-filtered for quest ticks; global for open-ended.
    recent_digest = build_recent_tick_digest(
        agent, team_id=team_id if tick_kind == TickKind.QUEST_WORK else None
    )
    if recent_digest:
        playbook = f"{playbook}\n\n{recent_digest}"

    playbook, preload_tools = _append_notification_inbox(
        agent, playbook, preload_tools
    )

    if not is_within_active_hours(agent.config.heartbeat):
        playbook += (
            "\n\n**Note: You are outside active hours. "
            "Only check notifications unless something is urgent.**"
        )

    return HeartbeatTaskContext(
        playbook=playbook,
        team_id=team_id,
        source=source,
        tick_kind=tick_kind,
        framing_override=heartbeat_framing_for_kind(tick_kind.value),
        include_plans_index=tick_kind == TickKind.OPEN_ENDED,
        preload_tools=preload_tools,
        inbox=items,
    )


def parse_heartbeat_tick_summary(result: str | None) -> dict[str, Any]:
    """Parse the heartbeat tick-summary JSON from the final assistant message.

    Returns a normalized dict with keys: action, details, selected_priority,
    worth_remembering, memory_notes, is_pass.
    """
    parsed = parse_json_from_llm(result) if result else None
    if not isinstance(parsed, dict):
        return {
            "action": "unknown",
            "details": "",
            "selected_priority": None,
            "worth_remembering": True,
            "memory_notes": [],
            "is_pass": False,
        }

    action = str(parsed.get("action") or "").strip()
    is_pass = action.lower() in {"none", "pass", "noop", "no-op", "n/a", "skip"}
    details = str(parsed.get("details") or "").strip()

    selected_priority = parsed.get("selected_priority")
    try:
        selected_priority = (
            int(selected_priority) if selected_priority is not None else None
        )
        if selected_priority is not None and not (1 <= selected_priority <= 6):
            selected_priority = None
    except (TypeError, ValueError):
        selected_priority = None
    if is_pass:
        selected_priority = None

    raw_worth = parsed.get("worth_remembering", not is_pass)
    if isinstance(raw_worth, bool):
        worth = raw_worth
    elif isinstance(raw_worth, str):
        worth = raw_worth.strip().lower() in {"true", "1", "yes"}
    else:
        worth = bool(raw_worth)
    if is_pass:
        worth = False

    notes_raw = parsed.get("memory_notes")
    memory_notes: list[str] = []
    if isinstance(notes_raw, list) and worth:
        memory_notes = [
            str(n).strip() for n in notes_raw if str(n).strip()
        ][:4]

    return {
        "action": "none" if is_pass else (action or "unknown"),
        "details": details,
        "selected_priority": selected_priority,
        "worth_remembering": worth,
        "memory_notes": memory_notes,
        "is_pass": is_pass,
    }


async def run_heartbeat(agent: OuroAgent) -> Optional[str]:
    """Run a full heartbeat cycle, grouping its sub-runs under one tick id.

    A single tick may fire several agent runs (planning, review, action). A
    shared ``tick_id`` stamped on each lets the run log group them together.
    """
    from ..uuid_v7 import uuid7_str

    previous_tick_id = getattr(agent, "_current_tick_id", None)
    agent._current_tick_id = uuid7_str()
    try:
        return await _run_heartbeat_impl(agent)
    finally:
        agent._current_tick_id = previous_tick_id


async def _run_heartbeat_impl(agent: OuroAgent) -> Optional[str]:
    """Run a full heartbeat cycle: inbox, planning bookkeeping, and one run."""
    from .planning import (
        auto_approve_due_drafts,
        load_cursor,
        planning_due,
        run_planning_run,
    )
    from .profiles import RunMode

    hb_model = resolve_heartbeat_model(agent)
    refresh_heartbeat_platform_context(agent)
    servers = heartbeat_servers(agent)
    planning_cfg = agent.config.planning
    team_ids = _sorted_team_ids(agent)

    inbox = load_work_inbox(agent)
    logger.info("Work inbox: %d actionable item(s)", len(inbox))

    # --- Planning bookkeeping ---
    # Auto-approval always runs; a new plan is only published once the inbox
    # has drained, so actionable work is finished before new quests appear.
    # Waiting-only quests never hold the inbox open, so parked follow-ups
    # don't block new plans.
    if planning_cfg.enabled and team_ids:
        try:
            auto_approve_due_drafts(agent, team_ids)
        except Exception:
            logger.exception("Draft auto-approval failed")

        if not inbox:
            workspace = agent.config.agent.workspace
            block_reason = planning_budget_blocks(agent, team_ids)
            if block_reason:
                logger.info("Skipping planning: %s", block_reason)
            else:
                due_teams = [
                    tid
                    for tid in team_ids
                    if _planning_team_is_writable(agent, tid)
                    and planning_due(load_cursor(workspace, tid), planning_cfg.cadence)
                ]
                if due_teams and is_curiosity_window(agent.config.heartbeat):
                    logger.info(
                        "Skipping planning: curiosity window (wind-down beats)"
                    )
                elif due_teams and not has_future_heartbeat_in_active_window(
                    agent.config.heartbeat
                ):
                    logger.info(
                        "Skipping planning: no future heartbeat remains in active window"
                    )
                elif due_teams:
                    team_id = _select_planning_team_id(agent, due_teams)
                    if team_id:
                        logger.info(
                            "Planning cadence due for team %s; publishing a new plan quest",
                            team_id[:8],
                        )
                        return await run_planning_run(agent, hb_model, team_id, servers)
    elif planning_cfg.enabled:
        logger.info("No teams discovered — planning requires a team")

    ctx = build_heartbeat_task_context(agent, inbox=inbox)
    if not ctx.playbook:
        logger.info(
            "No heartbeat playbook found and no quest inbox work "
            "(checked team doc store, global doc store, and local HEARTBEAT.md)"
        )
        return None

    logger.info(
        "Running heartbeat: kind=%s source=%s team=%s",
        ctx.tick_kind.value,
        ctx.source,
        ctx.team_id[:8] if ctx.team_id else "none",
    )

    result = await agent.run(
        ctx.playbook,
        model_override=hb_model,
        mode=RunMode.HEARTBEAT,
        allowed_servers=servers,
        preload_tools=ctx.preload_tools,
        preserve_existing_usage=True,
        team_id=ctx.team_id,
        mode_framing_override=ctx.framing_override,
        heartbeat_tick_kind=ctx.tick_kind.value,
        include_plans_index=ctx.include_plans_index,
    )

    summary = parse_heartbeat_tick_summary(result)
    if summary["is_pass"]:
        logger.info("Heartbeat completed: no action taken")
        return None
    if summary["action"] != "unknown":
        logger.info("Heartbeat completed: action=%s", summary["action"])
    else:
        logger.info("Heartbeat completed (no structured result)")

    return result


# ---------------------------------------------------------------------------
# Force helpers (CLI entry points)
# ---------------------------------------------------------------------------


async def force_planning_heartbeat(
    agent: OuroAgent,
    goal: str = "",
    team_id: str | None = None,
) -> Optional[str]:
    """Force a planning run regardless of cadence/timing (CLI entry point).

    When *goal* is provided the plan is framed around achieving it.
    """
    from .planning import run_planning_run

    hb_model = resolve_heartbeat_model(agent)
    refresh_heartbeat_platform_context(agent)
    servers = heartbeat_servers(agent)

    available_team_ids = _sorted_team_ids(agent)
    selected_team_id = team_id or next(iter(available_team_ids), None)
    if team_id and available_team_ids and team_id not in available_team_ids:
        logger.info("Requested planning team %s was not found", team_id)
        return None
    if not selected_team_id:
        logger.info("No team available for forced planning run")
        return None

    return await run_planning_run(agent, hb_model, selected_team_id, servers, goal=goal)
