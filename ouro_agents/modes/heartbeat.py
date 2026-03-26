"""Heartbeat mode: scheduler, active hours, and orchestration.

The heartbeat is the agent's autonomous tick — it runs on a timer, loads
a playbook, integrates the planning cycle, and decides what to do next.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import HeartbeatConfig

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
    return f"period={start_str}–{end_str} ({tz_label}); now={state}"


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

    kwargs = {}
    if unit == "s":
        kwargs["seconds"] = val
    elif unit == "m":
        kwargs["minutes"] = val
    elif unit == "h":
        kwargs["hours"] = val
    elif unit == "d":
        kwargs["days"] = val

    trigger = IntervalTrigger(**kwargs)

    async def _run_heartbeat():
        active = is_within_active_hours(config)
        if not active:
            logger.info("Outside active hours, skipping heartbeat")
            return

        try:
            logger.info("Running heartbeat...")
            import ouro_agents.server as server_module

            server_module.last_heartbeat = datetime.utcnow()

            await agent.heartbeat()
        except Exception as e:
            logger.error("Heartbeat failed: %s", e)

    scheduler.add_job(_run_heartbeat, trigger)
    scheduler.start()
    logger.info("Started heartbeat scheduler: every %s", config.every)


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
        update_post_status,
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

    # --- Planning cycle integration ---
    if agent.config.planning.enabled:
        plan_store = PlanStore(agent.config.agent.workspace / "plans")
        planning_cfg = agent.config.planning

        # --- Default plan: cadence-driven lifecycle ---
        default_plan = plan_store.load_default()

        action = next_action(
            current=default_plan,
            cadence=planning_cfg.cadence,
            min_heartbeats=planning_cfg.min_heartbeats,
            review_window=planning_cfg.review_window,
            auto_approve=planning_cfg.auto_approve,
        )

        if action == "plan":
            if default_plan and default_plan.status == "active":
                if default_plan.all_items_complete:
                    plan_store.archive(
                        default_plan, ouro_client=agent._get_ouro_client()
                    )
                    return await run_planning_heartbeat(
                        agent, hb_model, plan_store, servers
                    )
                else:
                    return await run_planning_heartbeat(
                        agent, hb_model, plan_store, servers, continuation=default_plan
                    )
            return await run_planning_heartbeat(agent, hb_model, plan_store, servers)

        if action == "check_review":
            reviewed = await run_review_heartbeat(
                agent, hb_model, plan_store, default_plan, servers
            )
            if reviewed:
                default_plan = reviewed

        if (
            action == "execute"
            and default_plan
            and default_plan.status == "pending_review"
        ):
            default_plan.status = "active"
            default_plan.activated_at = datetime.now(timezone.utc).isoformat()
            plan_store.save(default_plan)
            update_post_status(agent._get_ouro_client(), default_plan)
            comment_on_plan(
                agent._get_ouro_client(),
                default_plan.post_id,
                "Review window elapsed with no feedback — plan auto-activated.",
            )
            logger.info(
                "Plan cycle %s auto-approved (review window elapsed)", default_plan.id
            )
            post_link = (
                f" [plan](asset:{default_plan.post_id})" if default_plan.post_id else ""
            )
            write_daily_log(
                agent.config.agent.workspace,
                f"[planning:auto-approved]{post_link} Plan activated without feedback",
                doc_store=agent.doc_store,
                agent_name=agent.config.agent.name,
            )

        if default_plan and default_plan.status == "active":
            default_plan.heartbeats_completed += 1
            plan_store.save(default_plan)

        # --- Goal plans: auto-approve / auto-complete ---
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
                    update_post_status(agent._get_ouro_client(), gp)
                    comment_on_plan(
                        agent._get_ouro_client(),
                        gp.post_id,
                        "Review window elapsed — goal plan auto-activated.",
                    )
                    logger.info("Goal plan %s auto-approved", gp.id)
            if gp.status == "active":
                gp.heartbeats_completed += 1
                if gp.all_items_complete:
                    plan_store.archive(gp, ouro_client=agent._get_ouro_client())
                    logger.info("Goal plan %s completed (all items done)", gp.id)
                else:
                    plan_store.save(gp)

    # Load the autonomous playbook (Ouro first, local fallback)
    playbook = None
    if agent.doc_store:
        playbook = agent.doc_store.read(f"HEARTBEAT:{agent.config.agent.name}") or None
    if not playbook:
        heartbeat_path = agent.config.agent.workspace / "HEARTBEAT.md"
        if not heartbeat_path.exists():
            return None
        playbook = heartbeat_path.read_text()

    if not is_within_active_hours(agent.config.heartbeat):
        playbook += (
            "\n\n**Note: You are outside active hours. "
            "Only check notifications unless something is urgent.**"
        )

    extra_tools = []
    preload_tools = []
    if agent.config.planning.enabled:
        all_active = plan_store.load_all_active()
        active_plans = [p for p in all_active if p.status == "active"]
        if active_plans:
            from ..subagents.profiles import HEARTBEAT_PREFLIGHT
            from ..subagents.preflight import parse_heartbeat_preflight_result
            from .planning import render_plan_context

            preflight_task = (
                f"## Playbook\n{playbook}\n\n"
                f"## Active Plans\n{render_all_plans_context(active_plans)}"
            )

            preflight_result = agent._run_subagent(
                HEARTBEAT_PREFLIGHT,
                preflight_task,
                run_id=getattr(agent, "_current_run_id", ""),
            )
            
            logger.info("Raw heartbeat preflight result text: %s", preflight_result.text)
            
            preflight = parse_heartbeat_preflight_result(preflight_result.text)
            
            logger.info(
                "Heartbeat preflight result: action=%s plan_id=%s reasoning=%s", 
                preflight.action, 
                preflight.plan_id, 
                preflight.reasoning
            )

            if preflight.action == "skip":
                logger.info("Heartbeat preflight chose to skip: %s", preflight.reasoning)
                return None

            if preflight.action == "work_on_plan" and preflight.plan_id:
                target_plan = next((p for p in active_plans if p.id.startswith(preflight.plan_id)), None)
                if target_plan:
                    logger.info("Heartbeat preflight chose plan %s: %s", target_plan.id[:8], preflight.reasoning)
                    playbook = (
                        f"You are executing a specific plan during this heartbeat.\n\n"
                        f"{render_plan_context(target_plan)}\n\n"
                        "Use the update_plan tool to mark items done/in_progress as you complete them.\n"
                        "IMPORTANT: If you complete the final item in a plan during this heartbeat, "
                        "you MUST use the `create_comment` tool to comment on the plan's original post "
                        "(using the post id shown above). Summarize the work you accomplished and include "
                        "links to any posts or assets you created."
                    )
                    extra_tools = make_plan_tools(plan_store, agent._get_ouro_client())
                    preload_tools = ["ouro:create_comment"]

    result = await agent.run(
        playbook,
        model_override=hb_model,
        mode=RunMode.HEARTBEAT,
        allowed_servers=servers,
        extra_tools=extra_tools,
        preload_tools=preload_tools,
    )

    try:
        json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
        parsed = json.loads(json_match.group(1) if json_match else result)
        if parsed.get("action") == "none":
            logger.info("Heartbeat: no action taken")
            return None
        logger.info("Heartbeat action: %s", parsed.get("action", "unknown"))
    except (json.JSONDecodeError, AttributeError):
        pass

    return result


# ---------------------------------------------------------------------------
# Force helpers (CLI entry points)
# ---------------------------------------------------------------------------


async def force_planning_heartbeat(agent: OuroAgent, goal: str = "") -> Optional[str]:
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

    plan_store = PlanStore(agent.config.agent.workspace / "plans")

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

    plan_store = PlanStore(agent.config.agent.workspace / "plans")
    current = plan_store.load_by_id(plan_id) if plan_id else plan_store.load_default()

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
