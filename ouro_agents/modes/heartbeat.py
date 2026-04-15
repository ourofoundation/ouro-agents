"""Heartbeat mode: scheduler, active hours, and orchestration.

The heartbeat is the agent's autonomous tick — it runs on a timer, loads
a playbook, integrates the planning cycle, and decides what to do next.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import HeartbeatConfig
from ..constants import parse_interval_seconds, parse_json_from_llm

if TYPE_CHECKING:
    from ..agent import OuroAgent

logger = logging.getLogger(__name__)


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
    if not config.active_hours or "start" not in config.active_hours or "end" not in config.active_hours:
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


def build_plan_execution_playbook(plan_context: str, min_heartbeats: int) -> str:
    """Instruction block for working on an active plan during one heartbeat."""
    guidance = (
        "Treat this heartbeat as a bounded work session. Make one meaningful slice "
        "of progress, then stop. Do not try to clear an entire multi-step plan in "
        "a single tick, and do not feel pressure to use all available steps."
    )
    if min_heartbeats > 1:
        guidance += (
            f" This planning cycle is expected to unfold across at least "
            f"{min_heartbeats} heartbeats before replanning, so leave room for "
            "later heartbeats unless the remaining work is genuinely tiny."
        )

    return (
        "You are executing a specific plan during this heartbeat.\n\n"
        f"{plan_context}\n\n"
        f"{guidance}\n\n"
        "Use the update_plan tool to mark items done/in_progress as you complete them.\n"
        "IMPORTANT: If you complete the final item in a plan during this heartbeat, "
        "you MUST use the `create_comment` tool to comment on the plan's original quest "
        "(using the quest id shown above). Summarize the work you accomplished and include "
        "links to any posts or assets you created."
    )


def _load_playbook(agent: "OuroAgent", heartbeat_doc_store) -> str | None:
    """Load the heartbeat playbook: team doc store → global doc store → local file."""
    playbook = None
    if heartbeat_doc_store:
        playbook = (
            heartbeat_doc_store.read(f"HEARTBEAT:{agent.config.agent.name}") or None
        )
    if not playbook and heartbeat_doc_store is not agent.doc_store and agent.doc_store:
        playbook = (
            agent.doc_store.read(f"HEARTBEAT:{agent.config.agent.name}") or None
        )
    if not playbook:
        heartbeat_path = agent.config.agent.workspace / "HEARTBEAT.md"
        if heartbeat_path.exists():
            playbook = heartbeat_path.read_text()
    return playbook


def _sorted_team_ids(agent: "OuroAgent") -> list[str]:
    if not agent.team_registry:
        return []
    return sorted(agent.team_registry.team_ids())


def _select_heartbeat_team_id(team_plan_stores: dict[str, object]) -> str | None:
    ranked: list[tuple[int, str]] = []
    priorities = {"active": 0, "pending_review": 1, "planning": 2}
    for team_id, store in team_plan_stores.items():
        default_plan = store.load_default()
        if default_plan:
            ranked.append((priorities.get(default_plan.status, 3), team_id))
    if ranked:
        ranked.sort()
        selected = ranked[0][1]
        logger.info(
            "Selected heartbeat team %s (plan status=%s, %d teams with plans)",
            selected[:8], ranked[0][0], len(ranked),
        )
        return selected
    fallback = next(iter(team_plan_stores), None)
    logger.info(
        "No teams have active plans; defaulting to team %s (%d teams total)",
        fallback[:8] if fallback else "none", len(team_plan_stores),
    )
    return fallback


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
                logger.info("Next heartbeat scheduled for: %s", job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        except Exception as e:
            logger.error("Heartbeat failed: %s", e)

    job = scheduler.add_job(
        _run_heartbeat, 
        trigger,
        next_run_time=trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    )
    scheduler.start()
    
    next_run = job.next_run_time if hasattr(job, "next_run_time") else None
    next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else "unknown"
    logger.info("Started heartbeat scheduler: every %s; %s; next_run=%s", config.every, format_active_period_status(config), next_run_str)


# ---------------------------------------------------------------------------
# Heartbeat orchestration (previously agent.heartbeat)
# ---------------------------------------------------------------------------


async def run_heartbeat(agent: OuroAgent) -> Optional[str]:
    """Run a full heartbeat cycle: planning integration, playbook, and run."""
    from ..memory.reflection import write_daily_log
    from .planning import (
        PlanStore,
        comment_on_plan,
        make_plan_tools,
        next_action,
        parse_cadence_seconds,
        render_all_plans_context,
        run_planning_heartbeat,
        run_review_heartbeat,
        update_quest_status,
    )
    from .profiles import RunMode

    hb_model_id = agent.config.heartbeat.model or agent.config.agent.model
    hb_model = agent._build_model(hb_model_id, heartbeat=True)

    try:
        agent._refresh_platform_context()
    except Exception as e:
        logger.warning("Failed to refresh platform context during heartbeat: %s", e)

    proactive_cfg = agent.config.heartbeat.proactive
    servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]
    heartbeat_team_id: str | None = None
    heartbeat_doc_store = agent.doc_store
    team_plan_stores: dict[str, PlanStore] = {}
    plan_store: Optional[PlanStore] = None
    planning_cfg = agent.config.planning

    # --- Planning cycle integration ---
    if planning_cfg.enabled:
        logger.info(
            "Planning enabled: cadence=%s, min_heartbeats=%d, auto_approve=%s",
            planning_cfg.cadence, planning_cfg.min_heartbeats, planning_cfg.auto_approve,
        )
        workspace = agent.config.agent.workspace

        # Build per-team PlanStores and choose one team context for this tick.
        for tid in _sorted_team_ids(agent):
            team_plan_stores[tid] = PlanStore(
                workspace / "teams" / tid / "plans", team_id=tid,
            )

        # Migrate: if old flat workspace/plans/ exists, only auto-move when there
        # is a single team — otherwise the first sorted id is arbitrary and pins
        # plans to the wrong team.
        legacy_plans = workspace / "plans"
        sorted_team_ids = _sorted_team_ids(agent)
        if legacy_plans.exists() and (legacy_plans / "active").exists():
            if len(sorted_team_ids) != 1:
                logger.warning(
                    "Legacy workspace/plans/ exists but agent has %d teams; "
                    "skipping auto-migration. Move contents to teams/<team_id>/plans/ "
                    "manually or remove legacy plans/.",
                    len(sorted_team_ids),
                )
            else:
                first_tid = sorted_team_ids[0]
                import shutil

                dest = workspace / "teams" / first_tid / "plans"
                if not dest.exists():
                    dest.mkdir(parents=True, exist_ok=True)
                    for child in legacy_plans.iterdir():
                        if child.name in ("active", "history"):
                            shutil.copytree(child, dest / child.name, dirs_exist_ok=True)
                    shutil.rmtree(legacy_plans, ignore_errors=True)
                    logger.info("Migrated legacy plans/ → teams/%s/plans/", first_tid)
                    team_plan_stores[first_tid] = PlanStore(dest, team_id=first_tid)

        if not team_plan_stores:
            logger.warning("No teams discovered — cannot run planning without a team")
            return None

        heartbeat_team_id = _select_heartbeat_team_id(team_plan_stores)
        plan_store = team_plan_stores[heartbeat_team_id]
        default_plan = plan_store.load_default()
        heartbeat_doc_store = agent.doc_store_for(heartbeat_team_id)

        logger.info(
            "Default plan for team %s: %s",
            heartbeat_team_id[:8] if heartbeat_team_id else "none",
            f"id={default_plan.id[:8]} status={default_plan.status} items={len(default_plan.items)}"
            if default_plan else "none",
        )

        action = next_action(
            current=default_plan,
            cadence=planning_cfg.cadence,
            min_heartbeats=planning_cfg.min_heartbeats,
            review_window=planning_cfg.review_window,
            auto_approve=planning_cfg.auto_approve,
        )
        logger.info("Planning next_action=%s", action)

        if action == "plan":
            future_hb = has_future_heartbeat_in_active_window(agent.config.heartbeat)
            if not future_hb:
                if default_plan and default_plan.status == "active":
                    if default_plan.needs_replan_stale_active or default_plan.all_items_complete:
                        stale_window = default_plan.needs_replan_stale_active
                        plan_store.archive(
                            default_plan, ouro_client=agent._get_ouro_client()
                        )
                        reason = (
                            "stale (no quest/items)"
                            if stale_window
                            else "complete"
                        )
                        logger.info(
                            "Archived default plan %s at end of active window (%s)",
                            default_plan.id[:8],
                            reason,
                        )
                logger.info(
                    "Skipping planning: no future heartbeat remains in active window"
                )
                return None
            if default_plan and default_plan.status == "active":
                if default_plan.needs_replan_stale_active:
                    logger.info(
                        "Archiving defunct active plan %s (no quest, no items); "
                        "starting fresh planning cycle",
                        default_plan.id[:8],
                    )
                    plan_store.archive(
                        default_plan, ouro_client=agent._get_ouro_client()
                    )
                    return await run_planning_heartbeat(
                        agent, hb_model, plan_store, servers
                    )
                if default_plan.all_items_complete:
                    logger.info(
                        "Plan %s complete; archiving and starting fresh planning cycle",
                        default_plan.id[:8],
                    )
                    plan_store.archive(
                        default_plan, ouro_client=agent._get_ouro_client()
                    )
                    return await run_planning_heartbeat(
                        agent, hb_model, plan_store, servers
                    )
                logger.info(
                    "Continuing planning for active plan %s (%d/%d items done)",
                    default_plan.id[:8],
                    default_plan.items_done,
                    len(default_plan.items),
                )
                return await run_planning_heartbeat(
                    agent, hb_model, plan_store, servers, continuation=default_plan
                )
            logger.info("No existing plan; starting fresh planning cycle")
            return await run_planning_heartbeat(agent, hb_model, plan_store, servers)

        if action == "check_review":
            logger.info("Checking for review feedback on plan %s",
                        default_plan.id[:8] if default_plan else "none")
            reviewed = await run_review_heartbeat(
                agent, hb_model, plan_store, default_plan, servers
            )
            if reviewed:
                logger.info("Plan %s approved after review", reviewed.id[:8])
                default_plan = reviewed

        if (
            action == "execute"
            and default_plan
            and default_plan.status == "pending_review"
        ):
            default_plan.status = "active"
            default_plan.activated_at = datetime.now(timezone.utc).isoformat()
            plan_store.save(default_plan)
            update_quest_status(agent._get_ouro_client(), default_plan)
            comment_on_plan(
                agent._get_ouro_client(),
                default_plan.quest_id,
                "Review window elapsed with no feedback — plan auto-activated.",
            )
            logger.info(
                "Plan %s auto-approved (review window elapsed)", default_plan.id[:8]
            )
            quest_link = (
                f" [plan](asset:{default_plan.quest_id})" if default_plan.quest_id else ""
            )
            write_daily_log(
                agent.config.agent.workspace,
                f"[planning:auto-approved]{quest_link} Plan activated without feedback",
                doc_store=heartbeat_doc_store,
                agent_name=agent.config.agent.name,
            )

        if default_plan and default_plan.status == "active":
            default_plan.heartbeats_completed += 1
            plan_store.save(default_plan)
            logger.info(
                "Plan %s: heartbeats_completed=%d",
                default_plan.id[:8], default_plan.heartbeats_completed,
            )

        # --- Goal plans: auto-approve / auto-complete (selected team only) ---
        now_utc = datetime.now(timezone.utc)
        review_secs = parse_cadence_seconds(planning_cfg.review_window)
        for gp in plan_store.load_all_active():
            if gp.kind != "goal":
                continue
            if (
                gp.status == "pending_review"
                and planning_cfg.auto_approve
                and review_secs
            ):
                created = datetime.fromisoformat(gp.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if (now_utc - created).total_seconds() >= review_secs:
                    gp.status = "active"
                    gp.activated_at = now_utc.isoformat()
                    plan_store.save(gp)
                    update_quest_status(agent._get_ouro_client(), gp)
                    comment_on_plan(
                        agent._get_ouro_client(),
                        gp.quest_id,
                        "Review window elapsed — goal plan auto-activated.",
                    )
                    logger.info("Goal plan %s auto-approved", gp.id[:8])
            if gp.status == "active":
                gp.heartbeats_completed += 1
                if gp.all_items_complete:
                    plan_store.archive(gp, ouro_client=agent._get_ouro_client())
                    logger.info("Goal plan %s completed (all items done)", gp.id[:8])
                else:
                    plan_store.save(gp)
    else:
        logger.info("Planning disabled; skipping planning cycle")

    # --- Check for active plans that need execution ---
    extra_tools = []
    preload_tools = []
    playbook = None
    heartbeat_source = "none"

    if planning_cfg.enabled and plan_store:
        scoped_store = team_plan_stores.get(heartbeat_team_id, plan_store)
        active_plans = [
            p for p in scoped_store.load_all_active() if p.status == "active"
        ]
        logger.info(
            "Active plans for execution: %d",
            len(active_plans),
        )
        if active_plans:
            from ..subagents.profiles import HEARTBEAT_PREFLIGHT
            from ..subagents.preflight import parse_heartbeat_preflight_result
            from .planning import render_plan_context

            playbook_for_preflight = _load_playbook(agent, heartbeat_doc_store)

            preflight_context = f"## Active Plans\n{render_all_plans_context(active_plans)}"
            if playbook_for_preflight:
                preflight_context = f"## Playbook\n{playbook_for_preflight}\n\n{preflight_context}"

            logger.info("Running heartbeat preflight with %d active plan(s)...", len(active_plans))
            preflight_result = agent._run_subagent(
                HEARTBEAT_PREFLIGHT,
                preflight_context,
                run_id=getattr(agent, "_current_run_id", ""),
                team_id=heartbeat_team_id,
                doc_store=heartbeat_doc_store,
            )

            preflight = parse_heartbeat_preflight_result(preflight_result.text)
            logger.info(
                "Preflight decision: action=%s plan_id=%s reasoning=%s",
                preflight.action, preflight.plan_id, preflight.reasoning,
            )

            if preflight.action == "skip":
                logger.info("Heartbeat skipped by preflight: %s", preflight.reasoning)
                return None

            if preflight.action == "work_on_plan" and preflight.plan_id:
                target_plan = next(
                    (p for p in active_plans if p.id.startswith(preflight.plan_id)),
                    None,
                )
                if target_plan:
                    logger.info(
                        "Executing plan %s (%d items, %d done)",
                        target_plan.id[:8],
                        len(target_plan.items),
                        target_plan.items_done,
                    )
                    playbook = build_plan_execution_playbook(
                        render_plan_context(target_plan),
                        planning_cfg.min_heartbeats,
                    )
                    heartbeat_source = f"plan:{target_plan.id[:8]}"
                    target_store = (
                        team_plan_stores.get(target_plan.team_id, plan_store)
                        if target_plan.team_id
                        else plan_store
                    )
                    extra_tools = make_plan_tools(
                        target_store, agent._get_ouro_client()
                    )
                    preload_tools = ["ouro:create_comment"]
                else:
                    logger.warning(
                        "Preflight chose plan_id=%s but no matching active plan found",
                        preflight.plan_id,
                    )

    # If no plan was selected, load the general playbook
    if not playbook:
        playbook = _load_playbook(agent, heartbeat_doc_store)
        if playbook:
            heartbeat_source = "playbook"
    if not playbook:
        logger.info(
            "No heartbeat playbook found and no active plan to execute "
            "(checked team doc store, global doc store, and local HEARTBEAT.md)"
        )
        return None

    if not is_within_active_hours(agent.config.heartbeat):
        playbook += (
            "\n\n**Note: You are outside active hours. "
            "Only check notifications unless something is urgent.**"
        )

    logger.info(
        "Running heartbeat: source=%s, team=%s, servers=%s",
        heartbeat_source,
        heartbeat_team_id[:8] if heartbeat_team_id else "none",
        servers,
    )

    result = await agent.run(
        playbook,
        model_override=hb_model,
        mode=RunMode.HEARTBEAT,
        allowed_servers=servers,
        extra_tools=extra_tools,
        preload_tools=preload_tools,
        preserve_existing_usage=True,
        team_id=heartbeat_team_id,
    )

    parsed = parse_json_from_llm(result)
    if parsed:
        action_taken = parsed.get("action", "unknown")
        if action_taken == "none":
            logger.info("Heartbeat completed: no action taken")
            return None
        logger.info("Heartbeat completed: action=%s", action_taken)
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
    """Force a planning cycle regardless of cadence/timing (CLI entry point).

    When *goal* is provided the plan is framed around achieving it.
    """
    from .planning import PlanStore, run_planning_heartbeat

    hb_model_id = agent.config.heartbeat.model or agent.config.agent.model
    hb_model = agent._build_model(hb_model_id, heartbeat=True)

    try:
        agent._refresh_platform_context()
    except Exception as e:
        logger.warning("Failed to refresh platform context: %s", e)

    proactive_cfg = agent.config.heartbeat.proactive
    servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

    available_team_ids = _sorted_team_ids(agent)
    selected_team_id = team_id or next(iter(available_team_ids), None)
    if team_id and available_team_ids and team_id not in available_team_ids:
        logger.info("Requested planning team %s was not found", team_id)
        return None
    if not selected_team_id:
        logger.info("No team-scoped plan store available for forced planning heartbeat")
        return None
    plan_store = PlanStore(
        agent.config.agent.workspace / "teams" / selected_team_id / "plans",
        team_id=selected_team_id,
    )

    if goal:
        return await run_planning_heartbeat(
            agent,
            hb_model,
            plan_store,
            servers,
            goal=goal,
            kind="goal",
        )
    else:
        default = plan_store.load_default()
        if default and default.status == "active":
            plan_store.archive(default, ouro_client=agent._get_ouro_client())
        return await run_planning_heartbeat(agent, hb_model, plan_store, servers)


async def force_review_heartbeat(
    agent: OuroAgent, plan_id: str | None = None
) -> Optional[str]:
    """Force a review check on a selected plan (CLI entry point)."""
    from .planning import PlanStore, run_review_heartbeat

    workspace = agent.config.agent.workspace
    plan_store: PlanStore | None = None
    current = None

    for tid in _sorted_team_ids(agent):
        ps = PlanStore(workspace / "teams" / tid / "plans", team_id=tid)
        match = ps.load_by_id(plan_id) if plan_id else ps.load_default()
        if match:
            plan_store = ps
            current = match
            break

    if not current or current.status not in ("pending_review", "active"):
        logger.info("No plan cycle to review")
        return None

    hb_model_id = agent.config.heartbeat.model or agent.config.agent.model
    hb_model = agent._build_model(hb_model_id, heartbeat=True)

    try:
        agent._refresh_platform_context()
    except Exception as e:
        logger.warning("Failed to refresh platform context: %s", e)

    proactive_cfg = agent.config.heartbeat.proactive
    servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

    plan_text_before = current.plan_text
    reviewed = await run_review_heartbeat(agent, hb_model, plan_store, current, servers)
    if reviewed:
        return f"Plan approved and activated.\n\n{reviewed.plan_text}"
    reloaded = plan_store.load_by_id(current.id)
    if reloaded and reloaded.plan_text != plan_text_before:
        return f"Plan revised (pending approval, revision {reloaded.revision_count}).\n\n{reloaded.plan_text}"
    if reloaded:
        status = reloaded.status.replace("_", " ")
        return f"No feedback found - plan remains {status}."
    return "No feedback found - plan was not updated."
