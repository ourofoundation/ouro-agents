"""Dry-run heartbeat task assembly for an agent without executing the tick.

Rebuilds the exact task context a real heartbeat would hand to the strong
single-mode heartbeat (inbox playbook or general playbook + work direction +
shared snapshot + recent digest), prints tick kind / framing / preloads, and
optionally the full assembled task.

Usage:
    python scripts/dry_run_heartbeat.py --config hermes.json [--show-task]
"""

from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="hermes.json")
    parser.add_argument("--show-task", action="store_true")
    parser.add_argument("--show-framing", action="store_true")
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
        f"=== Heartbeat context: kind={ctx.tick_kind.value} source={source} "
        f"team={ctx.team_id or 'none'} include_plans_index={ctx.include_plans_index} ===",
        flush=True,
    )
    if not is_within_active_hours(agent.config.heartbeat):
        print(
            "NOTE: Outside configured active hours. The scheduler would skip this "
            "tick; continuing only as a context dry-run.",
            flush=True,
        )
    if not ctx.playbook:
        print("No playbook or inbox work — heartbeat would do nothing.", flush=True)
        agent.close()
        sys.exit(0)

    print(f"Task context: {len(ctx.playbook)} chars", flush=True)
    print(f"Preload tools: {', '.join(ctx.preload_tools) or '(none)'}", flush=True)
    profile = resolve_mode_profile(RunMode.HEARTBEAT)
    print(f"Profile max_steps: {profile.max_steps}", flush=True)

    if args.show_framing:
        print(
            "\n----- MODE FRAMING -----\n"
            + (ctx.framing_override or profile.framing)
            + "\n----- END FRAMING -----\n",
            flush=True,
        )
    if args.show_task:
        print(
            "\n----- TASK CONTEXT -----\n" + ctx.playbook + "\n----- END TASK -----\n",
            flush=True,
        )

    agent.close()


if __name__ == "__main__":
    main()
