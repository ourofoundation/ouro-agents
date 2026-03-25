import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import OuroAgentsConfig, RunMode
from .agent import OuroAgent
from .display import OuroDisplay, Verbosity, set_display
from .server import start_server


def _resolve_verbosity(args: argparse.Namespace) -> Verbosity:
    if getattr(args, "quiet", False):
        return Verbosity.QUIET
    if getattr(args, "verbose", False):
        return Verbosity.VERBOSE
    return Verbosity.NORMAL


def _run_chat(config_path: str, conversation_id: str | None, display: OuroDisplay) -> int:
    config = OuroAgentsConfig.load_from_file(config_path)

    if not conversation_id:
        conversation_id = str(uuid4())

    display.chat_header(conversation_id)

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
                )
            )
            display.chat_response(result)


def main():
    parser = argparse.ArgumentParser(description="Ouro Agents CLI")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
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
    subparsers.add_parser("plan", help="Force a planning heartbeat (generate a new plan cycle)")
    subparsers.add_parser("review", help="Force a review heartbeat (check for feedback on current plan)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    verbosity = _resolve_verbosity(args)
    display = OuroDisplay(verbosity)
    set_display(display)

    if args.command == "serve":
        start_server(args.config)
    elif args.command == "run":
        config = OuroAgentsConfig.load_from_file(args.config)
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
            result = asyncio.run(agent.run(args.task, debug_markdown_path=debug_md_path))
        display.run_result(result)
        if debug_md_path is not None:
            display.info(f"Debug markdown written to {debug_md_path}")
    elif args.command == "chat":
        sys.exit(_run_chat(args.config, args.conversation_id, display))
    elif args.command == "heartbeat":
        config = OuroAgentsConfig.load_from_file(args.config)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.heartbeat())
        display.heartbeat_result(result)
    elif args.command == "plan":
        config = OuroAgentsConfig.load_from_file(args.config)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.force_planning_heartbeat())
        display.planning_result(result)
    elif args.command == "review":
        config = OuroAgentsConfig.load_from_file(args.config)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.force_review_heartbeat())
        display.review_result(result)

if __name__ == "__main__":
    main()
