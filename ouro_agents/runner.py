import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from typing import Optional

from .agent import OuroAgent
from .config import OuroAgentsConfig, RunMode
from .display import OuroDisplay, Verbosity, set_display
from .modes.planning import PlanStore
from .observer import AgentObserver
from .server import start_server
from .tui.review_picker import choose_review_plan, reviewable_plans
from .tui.team_picker import choose_plan_team


def _resolve_verbosity(args: argparse.Namespace) -> Verbosity:
    if getattr(args, "quiet", False):
        return Verbosity.QUIET
    if getattr(args, "verbose", False):
        return Verbosity.VERBOSE
    return Verbosity.NORMAL


class CLIAgentObserver(AgentObserver):
    def __init__(self, display: OuroDisplay):
        self.display = display

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        pass

    def on_stream_chunk(self, chunk: str) -> None:
        pass

    def on_result_ready(self, result_text: str) -> None:
        pass

    def on_step_persist(self, step: dict) -> None:
        pass

    def on_reasoning_persist(self, content: str) -> None:
        pass


def _run_chat(
    config_path: str, conversation_id: str | None, display: OuroDisplay
) -> int:
    config = OuroAgentsConfig.load_from_file(config_path)

    if not conversation_id:
        conversation_id = str(uuid4())

    display.chat_header(conversation_id)
    observer = CLIAgentObserver(display)

    with OuroAgent(config) as agent:
        while True:
            user_input = display.prompt()
            if not user_input:
                display.info("Exiting.")
                return 0

            if user_input in {"/exit", "/quit"}:
                display.info("Exiting.")
                return 0

            if user_input == "/new":
                conversation_id = str(uuid4())
                display.info(f"New conversation: {conversation_id}")
                continue

            if user_input.startswith("/conversation "):
                next_id = user_input.replace("/conversation ", "", 1).strip()
                if not next_id:
                    display.error("Please provide a conversation id.")
                    continue
                conversation_id = next_id
                display.info(f"Switched to: {conversation_id}")
                continue

            result = asyncio.run(
                agent.run(
                    user_input,
                    conversation_id=conversation_id,
                    mode=RunMode.CHAT,
                    user_id="creator",
                    observer=observer,
                )
            )
            display.chat_response(result)


def main():
    parser = argparse.ArgumentParser(description="Ouro Agents CLI")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--env-file", default=None, help="Path to .env file (default: .env)")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output (show debug info)"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Quiet output (errors only)"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("serve", help="Start the FastAPI server")

    run_parser = subparsers.add_parser("run", help="Run a single task")
    run_parser.add_argument("task", help="The task for the agent to perform")
    run_parser.add_argument(
        "--debug-md",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "Write the full system prompt and each agent step to a markdown file. "
            "With no PATH, uses <workspace>/debug-runs/run-<UTC timestamp>.md"
        ),
    )

    chat_parser = subparsers.add_parser("chat", help="Start interactive chat mode")
    chat_parser.add_argument(
        "--conversation-id",
        default=None,
        help="Resume an existing conversation by id",
    )

    subparsers.add_parser("heartbeat", help="Run a single heartbeat tick")
    plan_parser = subparsers.add_parser(
        "plan", help="Force a planning heartbeat (generate a new plan cycle)"
    )
    plan_parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Optional goal or directive the plan should be built around",
    )
    plan_parser.add_argument(
        "--team-id",
        default=None,
        help="Create the plan for a specific team id",
    )
    subparsers.add_parser(
        "review", help="Force a review heartbeat (check for feedback on current plan)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    import os

    if args.env_file:
        os.environ["ENV_FILE"] = args.env_file

    config = OuroAgentsConfig.load_from_file(args.config)
    verbosity = _resolve_verbosity(args)
    display = OuroDisplay(
        verbosity,
        show_reasoning_in_summary=config.display.usage_table.show_reasoning,
    )
    set_display(display)

    if args.command == "serve":
        start_server(args.config)
    elif args.command == "run":
        display.run_header(args.task)
        debug_md_path = None
        if getattr(args, "debug_md", None) is not None:
            ws = config.agent.workspace
            if args.debug_md == "":
                debug_md_path = (
                    ws
                    / "debug-runs"
                    / f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
                )
            else:
                debug_md_path = Path(args.debug_md).expanduser().resolve()
        with OuroAgent(config) as agent:
            result = asyncio.run(
                agent.run(args.task, debug_markdown_path=debug_md_path)
            )
        display.run_result(result)
        if debug_md_path is not None:
            display.info(f"Debug markdown written to {debug_md_path}")
    elif args.command == "chat":
        sys.exit(_run_chat(args.config, args.conversation_id, display))
    elif args.command == "heartbeat":
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.heartbeat())
        display.heartbeat_result(result)
    elif args.command == "plan":
        with OuroAgent(config) as agent:
            try:
                agent._refresh_platform_context()
            except Exception:
                pass

            selected_team_id = args.team_id
            if selected_team_id and not agent.team_registry.get_team(selected_team_id):
                display.error(f"Unknown team id: {selected_team_id}")
                sys.exit(1)

            if not selected_team_id:
                selected_team_id = choose_plan_team(agent.team_registry.list_teams())
                if agent.team_registry.list_teams() and selected_team_id is None:
                    display.info("Planning cancelled.")
                    sys.exit(0)

            if not selected_team_id:
                display.info("planning: no team available")
                sys.exit(1)

            result = asyncio.run(
                agent.force_planning_heartbeat(
                    goal=args.prompt,
                    team_id=selected_team_id,
                )
            )
        display.planning_result(result)
    elif args.command == "review":
        from .teams import TeamRegistry

        team_reg = TeamRegistry.from_platform_context(
            config.agent.workspace, config.agent.org_id,
        )
        all_active = []
        for tid in sorted(team_reg.team_ids()):
            ps = PlanStore(config.agent.workspace / "teams" / tid / "plans", team_id=tid)
            all_active.extend(ps.load_all_active())
        selected_plan_id = choose_review_plan(reviewable_plans(all_active))
        if all_active and selected_plan_id is None:
            display.info("Review cancelled.")
            sys.exit(0)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.force_review_heartbeat(plan_id=selected_plan_id))
        display.review_result(result)


if __name__ == "__main__":
    main()
