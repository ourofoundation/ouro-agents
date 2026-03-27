import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .agent import OuroAgent
from .config import OuroAgentsConfig, RunMode
from .display import OuroDisplay, Verbosity, set_display
from .modes.planning import PlanStore
from .server import start_server
from .tui.review_picker import choose_review_plan, reviewable_plans


def _resolve_verbosity(args: argparse.Namespace) -> Verbosity:
    if getattr(args, "quiet", False):
        return Verbosity.QUIET
    if getattr(args, "verbose", False):
        return Verbosity.VERBOSE
    return Verbosity.NORMAL


def _run_chat(
    config_path: str, conversation_id: str | None, display: OuroDisplay
) -> int:
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
    plan_parser = subparsers.add_parser(
        "plan", help="Force a planning heartbeat (generate a new plan cycle)"
    )
    plan_parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Optional goal or directive the plan should be built around",
    )
    subparsers.add_parser(
        "review", help="Force a review heartbeat (check for feedback on current plan)"
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-memory",
        help="Create shared team and seed Ouro posts from local workspace files",
    )
    bootstrap_parser.add_argument(
        "--team-id",
        default=None,
        help="Use an existing team instead of creating one",
    )
    bootstrap_parser.add_argument(
        "--org-id",
        default=None,
        help="Override org_id from config",
    )

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
            result = asyncio.run(
                agent.run(args.task, debug_markdown_path=debug_md_path)
            )
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
            result = asyncio.run(agent.force_planning_heartbeat(goal=args.prompt))
        display.planning_result(result)
    elif args.command == "review":
        config = OuroAgentsConfig.load_from_file(args.config)
        plan_store = PlanStore(config.agent.workspace / "plans")
        selected_plan_id = choose_review_plan(
            reviewable_plans(plan_store.load_all_active())
        )
        if plan_store.load_all_active() and selected_plan_id is None:
            display.info("Review cancelled.")
            sys.exit(0)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.force_review_heartbeat(plan_id=selected_plan_id))
        display.review_result(result)
    elif args.command == "bootstrap-memory":
        config = OuroAgentsConfig.load_from_file(args.config)
        _bootstrap_memory(config, args, display)


def _ensure_team_membership(tools: dict, team_id: str, display: OuroDisplay) -> None:
    """Join the team if the agent is not already a member."""
    get_teams = tools.get("ouro:get_teams")
    if not get_teams:
        display.info("Warning: get_teams tool unavailable, skipping membership check")
        return

    try:
        raw = get_teams()
        data = json.loads(raw) if isinstance(raw, str) else raw
        joined = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(joined, list):
            for team in joined:
                if team.get("id") == team_id:
                    display.info(f"Already a member of team {team_id}")
                    return
    except Exception as e:
        display.info(f"Warning: could not check team membership: {e}")

    join_team = tools.get("ouro:join_team")
    if not join_team:
        display.info("Warning: join_team tool unavailable, skipping auto-join")
        return

    try:
        join_team(id=team_id)
        display.info(f"Joined team {team_id}")
    except Exception as e:
        display.info(f"Warning: failed to join team {team_id}: {e}")


def _bootstrap_memory(
    config: OuroAgentsConfig, args: argparse.Namespace, display: OuroDisplay
) -> None:
    """Create shared team and seed Ouro posts from local workspace files."""
    with OuroAgent(config) as agent:
        tools = agent._deferred_tools
        org_id = args.org_id or config.agent.org_id
        if not org_id:
            display.error(
                "No org_id configured. Set agent.org_id in config.json or pass --org-id."
            )
            sys.exit(1)

        team_id = args.team_id or config.agent.team_id

        if not team_id:
            display.info("Creating shared agent team...")
            raw = tools.get("ouro:create_team")
            if not raw:
                display.error(
                    "ouro:create_team tool not available. Is the Ouro MCP server connected?"
                )
                sys.exit(1)
            result = raw(
                name="agent-memory",
                org_id=org_id,
                description="Shared memory space for Ouro agents",
                visibility="organization",
                source_policy="api_only",
            )
            data = json.loads(result) if isinstance(result, str) else result
            team_id = data.get("id") or data.get("team_id")
            if not team_id:
                display.error(f"Failed to create team: {result}")
                sys.exit(1)
            display.info(f"Created team: {team_id}")

        # Ensure the agent has joined the team
        _ensure_team_membership(tools, team_id, display)

        from .memory.ouro_docs import OuroDocStore

        doc_store = OuroDocStore(
            agent_name=config.agent.name,
            org_id=org_id,
            team_id=team_id,
        )

        name = config.agent.name
        ws = config.agent.workspace
        seeded = []

        file_map = {
            f"SOUL:{name}": ws / "SOUL.md",
            f"HEARTBEAT:{name}": ws / "HEARTBEAT.md",
            f"NOTES:{name}": ws / "NOTES.md",
            f"MEMORY:{name}": ws / "MEMORY.md",
        }

        for post_name, local_path in file_map.items():
            if local_path.exists():
                content = local_path.read_text().strip()
                if content:
                    doc_store.write(post_name, content)
                    seeded.append(post_name)
                    display.info(f"  Seeded {post_name} from {local_path.name}")

        daily_dir = ws / "memory" / "daily"
        if daily_dir.exists():
            for md_file in sorted(daily_dir.glob("*.md")):
                day = md_file.stem
                content = md_file.read_text().strip()
                if content:
                    post_name = f"DAILY:{name}:{day}"
                    doc_store.write(post_name, content)
                    seeded.append(post_name)
            if any(p.startswith("DAILY:") for p in seeded):
                display.info(f"  Seeded daily logs")

        users_dir = ws / "memory" / "users"
        if users_dir.exists():
            for md_file in users_dir.glob("*.md"):
                user_id = md_file.stem
                content = md_file.read_text().strip()
                if content:
                    post_name = f"USER:{user_id}"
                    doc_store.write(post_name, content)
                    seeded.append(post_name)
                    display.info(f"  Seeded {post_name}")

        display.info(f"\nBootstrap complete: {len(seeded)} posts created.")
        display.info(f"Team ID: {team_id}")
        display.info(f'Add to config.json → agent.team_id: "{team_id}"')


if __name__ == "__main__":
    main()
