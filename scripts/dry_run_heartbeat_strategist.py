"""Dry-run the strong heartbeat strategist for an agent without executing the tick.

Rebuilds the exact task context a real heartbeat would hand to the strategist
(inbox playbook or general playbook + work direction + shared snapshot), then
runs only the strategist subagent (read-only tools — no side effects) and
prints the parsed brief plus the execution brief the mid-tier executor would
receive.

Usage:
    python scripts/dry_run_heartbeat_strategist.py --config hermes.json [-n 2]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

logging.basicConfig(level=logging.WARNING)

from ouro_agents.agent import OuroAgent
from ouro_agents.config import OuroAgentsConfig
from ouro_agents.modes.heartbeat import (
    build_heartbeat_task_context,
    is_within_active_hours,
    refresh_heartbeat_platform_context,
)
from ouro_agents.modes.profiles import resolve_mode_profile, RunMode
from ouro_agents.subagents.strategist import format_heartbeat_execution_brief


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="hermes.json")
    parser.add_argument("-n", "--runs", type=int, default=1)
    parser.add_argument("--show-task", action="store_true")
    args = parser.parse_args()

    config = OuroAgentsConfig.load_from_file(args.config)
    agent = OuroAgent(config)
    agent.connect_mcp()

    refresh_heartbeat_platform_context(agent)
    ctx = build_heartbeat_task_context(agent, advance_recurring=False)
    source = ctx.source
    if ctx.source == "quest-inbox":
        source = f"quest-inbox ({len(ctx.inbox)} items)"
    print(
        f"=== Heartbeat context source: {source}; team={ctx.team_id or 'none'} ===",
        flush=True,
    )
    if not is_within_active_hours(agent.config.heartbeat):
        print(
            "NOTE: Outside configured active hours. The scheduler would skip this "
            "tick; continuing only as a strategist simulation.",
            flush=True,
        )
    if not ctx.playbook:
        print("No playbook or inbox work — heartbeat would do nothing.", flush=True)
        sys.exit(0)
    print(f"Task context: {len(ctx.playbook)} chars", flush=True)
    if args.show_task:
        print(
            "\n----- TASK CONTEXT -----\n" + ctx.playbook + "\n----- END TASK -----\n",
            flush=True,
        )

    profile = resolve_mode_profile(RunMode.HEARTBEAT)
    _, eligible_index = agent._filter_deferred_for_profile(profile, ["ouro"])
    available_tools = [item["tool"] for item in eligible_index]

    for i in range(args.runs):
        print(
            f"\n########## STRATEGIST RUN {i + 1}/{args.runs} ##########",
            flush=True,
        )
        result = agent._run_strategist(
            ctx.playbook,
            allowed_capabilities=profile.allowed_capabilities,
            preload_tools=list(profile.preload_tools),
            available_tools=available_tools,
            team_id=ctx.team_id,
        )
        print("\n--- Parsed strategist output ---")
        print(
            json.dumps(
                {
                    "objective": result.objective,
                    "selected_priority": result.selected_priority,
                    "priority_audit": result.priority_audit,
                    "worth_remembering": result.worth_remembering,
                    "briefing": result.briefing,
                    "actions": result.actions,
                    "evidence": result.evidence,
                    "stop_conditions": result.stop_conditions,
                    "tools": result.tools,
                    "prefetch_assets": result.prefetch_assets,
                    "memory_notes": result.memory_notes,
                    "is_pass": result.is_pass_objective,
                },
                indent=2,
            )
        )
        print("\n--- Execution brief the mid-tier executor would receive ---")
        print(format_heartbeat_execution_brief(result))

    agent.close()


if __name__ == "__main__":
    main()
