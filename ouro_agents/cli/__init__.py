from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from ..agent import OuroAgent
from ..cancellation import RunCancelled
from ..config import OuroAgentsConfig, RunMode
from ..display import OuroDisplay, Verbosity, set_display
from ..modes.planning import PlanStore
from ..server import start_server
from ..tui.review_picker import choose_review_plan, reviewable_plans
from ..tui.team_picker import _ALL_TEAMS_SENTINEL, choose_dream_team, choose_plan_team
from ..uuid_v7 import uuid7_str
from .auth import (
    DEFAULT_LOGIN_TIMEOUT,
    LoginTimeout,
    clear_user_credentials,
    credentials_path,
    get_agent_client,
    get_user_client,
    identify_account,
    login_and_identify,
    read_identity,
    resolved_base_url,
    save_user_credentials,
)
from .conversations import create_conversation, send_user_message
from .observer import TUIObserver


@dataclass
class CLIState:
    config_path: str
    config: OuroAgentsConfig
    display: OuroDisplay


cli = typer.Typer(
    name="ouro-agents",
    help="Ouro Agents CLI and Textual app.",
    invoke_without_command=True,
    no_args_is_help=False,
)

from .runs import runs_app  # noqa: E402

cli.add_typer(runs_app, name="runs")


def _verbosity(verbose: bool, quiet: bool) -> Verbosity:
    if quiet:
        return Verbosity.QUIET
    if verbose:
        return Verbosity.VERBOSE
    return Verbosity.NORMAL


def _state(ctx: typer.Context) -> CLIState:
    if not isinstance(ctx.obj, CLIState):
        raise typer.BadParameter("CLI state was not initialized.")
    return ctx.obj


@cli.callback()
def callback(
    ctx: typer.Context,
    config: str = typer.Option("config.json", "--config", help="Path to config.json"),
    env_file: Optional[str] = typer.Option(
        None, "--env-file", help="Path to .env file (default: .env)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Show errors only"),
) -> None:
    if env_file:
        os.environ["ENV_FILE"] = env_file
    loaded_config = OuroAgentsConfig.load_from_file(config)
    display = OuroDisplay(
        _verbosity(verbose, quiet),
        show_reasoning_in_summary=loaded_config.display.usage_table.show_reasoning,
    )
    set_display(display)
    ctx.obj = CLIState(config_path=config, config=loaded_config, display=display)
    if ctx.invoked_subcommand is None:
        _launch_app(loaded_config)


@cli.command("app")
def app_command(ctx: typer.Context) -> None:
    """Launch the activity-oriented Textual app."""
    _launch_app(_state(ctx).config)


@cli.command()
def login(
    api_key: Optional[str] = typer.Option(
        None, "--api-key", help="Personal Ouro API key/PAT. Prompts if omitted."
    ),
    base_url: Optional[str] = typer.Option(
        None, "--base-url", help="Ouro backend URL (defaults to OURO_BASE_URL)."
    ),
    timeout: float = typer.Option(
        DEFAULT_LOGIN_TIMEOUT,
        "--timeout",
        help="Seconds to wait for the Ouro backend before giving up.",
    ),
) -> None:
    """Store the personal Ouro API key used for the human side of the app."""
    try:
        key = api_key or typer.prompt("Personal Ouro API key", hide_input=True)
    except (KeyboardInterrupt, EOFError, typer.Abort):
        typer.echo("\nLogin cancelled.")
        raise typer.Exit(130)

    key = (key or "").strip()
    if not key:
        typer.echo("No API key provided.")
        raise typer.Exit(1)

    typer.echo(f"Validating API key against {resolved_base_url(base_url)}...")
    try:
        identity = login_and_identify(key, base_url=base_url, timeout=timeout)
    except LoginTimeout:
        typer.echo(
            f"Timed out after {timeout:.0f}s contacting {resolved_base_url(base_url)}. "
            "Check the backend is reachable (OURO_BASE_URL / --base-url) and retry."
        )
        raise typer.Exit(1)
    except KeyboardInterrupt:
        typer.echo("\nLogin cancelled.")
        raise typer.Exit(130)
    except Exception as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    path = save_user_credentials(api_key=key, base_url=base_url)
    typer.echo(f"Logged in as {identity.display_name} ({identity.actor_type}).")
    typer.echo(f"Credentials written to {path}")


@cli.command()
def logout() -> None:
    """Remove stored personal Ouro credentials."""
    removed = clear_user_credentials()
    if removed:
        typer.echo(f"Removed {credentials_path()}")
    else:
        typer.echo("No stored credentials found.")


@cli.command()
def whoami() -> None:
    """Show the configured personal and agent Ouro identities."""
    for label, factory in (("you", get_user_client), ("agent", get_agent_client)):
        try:
            identity = identify_account(factory)
            key_name = f" key={identity.api_key_name}" if identity.api_key_name else ""
            typer.echo(
                f"{label}: {identity.display_name} "
                f"id={identity.user_id} actor_type={identity.actor_type}{key_name}"
            )
        except Exception as exc:
            typer.echo(f"{label}: not configured ({exc})")


@cli.command()
def serve(ctx: typer.Context) -> None:
    """Start the FastAPI server."""
    start_server(_state(ctx).config_path)


@cli.command()
def run(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="The task for the agent to perform"),
    debug_md: Optional[Path] = typer.Option(
        None,
        "--debug-md",
        help="Write the system prompt and agent steps to this markdown file.",
    ),
) -> None:
    """Run a single autonomous task."""
    state = _state(ctx)
    display = state.display
    config = state.config
    display.run_header(task)
    debug_md_path = debug_md
    if debug_md_path is not None and str(debug_md_path) == "":
        debug_md_path = (
            config.agent.workspace
            / "debug-runs"
            / f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        )
    with OuroAgent(config) as agent:
        try:
            result = asyncio.run(agent.run(task, debug_markdown_path=debug_md_path))
        except (KeyboardInterrupt, RunCancelled):
            agent.cancel_active_runs("interrupted")
            display.info("Run cancelled.")
            raise typer.Exit(130)
    display.run_result(result)
    if debug_md_path is not None:
        display.info(f"Debug markdown written to {debug_md_path}")


@cli.command()
def chat(
    ctx: typer.Context,
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation-id", help="Resume an existing Ouro conversation by id."
    ),
) -> None:
    """Start an inline Ouro-backed chat REPL."""
    state = _state(ctx)
    raise_code = _run_inline_chat(state, conversation_id)
    raise typer.Exit(raise_code)


@cli.command()
def heartbeat(ctx: typer.Context) -> None:
    """Run a single heartbeat tick."""
    state = _state(ctx)
    with OuroAgent(state.config) as agent:
        try:
            result = asyncio.run(agent.heartbeat())
        except (KeyboardInterrupt, RunCancelled):
            agent.cancel_active_runs("interrupted")
            state.display.info("Heartbeat cancelled.")
            raise typer.Exit(130)
    state.display.heartbeat_result(result)


@cli.command()
def plan(
    ctx: typer.Context,
    prompt: str = typer.Argument(
        "",
        help="Optional goal or directive the plan should be built around.",
    ),
    team_id: Optional[str] = typer.Option(
        None, "--team-id", help="Create the plan for a specific team id."
    ),
) -> None:
    """Force a planning heartbeat."""
    state = _state(ctx)
    with OuroAgent(state.config) as agent:
        try:
            agent._refresh_platform_context()
        except Exception:
            pass

        selected_team_id = team_id
        if selected_team_id and not agent.team_registry.get_team(selected_team_id):
            state.display.error(f"Unknown team id: {selected_team_id}")
            raise typer.Exit(1)

        if not selected_team_id:
            selected_team_id = choose_plan_team(agent.team_registry.list_teams())
            if agent.team_registry.list_teams() and selected_team_id is None:
                state.display.info("Planning cancelled.")
                raise typer.Exit(0)

        if not selected_team_id:
            state.display.info("planning: no team available")
            raise typer.Exit(1)

        try:
            result = asyncio.run(
                agent.force_planning_heartbeat(goal=prompt, team_id=selected_team_id)
            )
        except (KeyboardInterrupt, RunCancelled):
            agent.cancel_active_runs("interrupted")
            state.display.info("Planning cancelled.")
            raise typer.Exit(130)
    state.display.planning_result(result)


@cli.command()
def review(ctx: typer.Context) -> None:
    """Force a review heartbeat."""
    state = _state(ctx)
    from ..teams import TeamRegistry

    team_reg = TeamRegistry.from_platform_context(
        state.config.agent.workspace,
        state.config.agent.org_id,
    )
    all_active = []
    for tid in sorted(team_reg.team_ids()):
        ps = PlanStore(state.config.agent.workspace / "teams" / tid / "plans", team_id=tid)
        all_active.extend(ps.load_all_active())
    selected_plan_id = choose_review_plan(reviewable_plans(all_active))
    if all_active and selected_plan_id is None:
        state.display.info("Review cancelled.")
        raise typer.Exit(0)
    with OuroAgent(state.config) as agent:
        try:
            result = asyncio.run(agent.force_review_heartbeat(plan_id=selected_plan_id))
        except (KeyboardInterrupt, RunCancelled):
            agent.cancel_active_runs("interrupted")
            state.display.info("Review cancelled.")
            raise typer.Exit(130)
    state.display.review_result(result)


@cli.command()
def dream(
    ctx: typer.Context,
    team_id: Optional[str] = typer.Option(
        None, "--team-id", help="Run dream for a specific team only."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview dream changes and write an audit log without mutating memory.",
    ),
) -> None:
    """Run the dream cycle."""
    state = _state(ctx)
    with OuroAgent(state.config) as agent:
        selected_team_id = team_id
        if not selected_team_id:
            try:
                agent._refresh_platform_context()
            except Exception:
                pass
            selected_team_id = choose_dream_team(agent.team_registry.list_teams())
            if selected_team_id is None:
                state.display.info("Dream cancelled.")
                raise typer.Exit(0)

        resolved_team_id = (
            None if selected_team_id == _ALL_TEAMS_SENTINEL else selected_team_id
        )
        scope = f" for team {resolved_team_id}" if resolved_team_id else " (all teams)"
        mode = "dry-run " if dry_run else ""
        state.display.info(f"Running {mode}dream cycle{scope}...")
        results = agent.dream(team_id=resolved_team_id, dry_run=dry_run)
    for scope, summary in results.items():
        state.display.info(f"  [{scope}] {summary}")


def _run_inline_chat(state: CLIState, conversation_id: str | None) -> int:
    display = state.display
    try:
        user_client = get_user_client()
        agent_client = get_agent_client()
        user_identity = read_identity(user_client)
        agent_identity = read_identity(agent_client)
    except Exception as exc:
        display.error(str(exc))
        return 1

    if not conversation_id:
        conversation = create_conversation(
            user_client,
            user_id=user_identity.user_id,
            agent_id=agent_identity.user_id,
            org_id=state.config.agent.org_id,
        )
        conversation_id = str(conversation.id)

    display.chat_header(conversation_id)
    display.info("This chat is backed by an Ouro conversation.")

    with OuroAgent(state.config) as agent:
        while True:
            user_input = display.prompt(user_identity.display_name or "you")
            if not user_input or user_input in {"/exit", "/quit"}:
                display.info("Exiting.")
                return 0

            if user_input == "/new":
                conversation = create_conversation(
                    user_client,
                    user_id=user_identity.user_id,
                    agent_id=agent_identity.user_id,
                    org_id=state.config.agent.org_id,
                )
                conversation_id = str(conversation.id)
                display.info(f"New Ouro conversation: {conversation_id}")
                continue

            if user_input.startswith("/conversation "):
                next_id = user_input.replace("/conversation ", "", 1).strip()
                if not next_id:
                    display.error("Please provide a conversation id.")
                    continue
                conversation_id = next_id
                display.info(f"Switched to: {conversation_id}")
                continue

            try:
                send_user_message(
                    user_client,
                    conversation_id,
                    user_id=user_identity.user_id,
                    text=user_input,
                )
                observer = TUIObserver(
                    emit=lambda event: None,
                    agent_client=agent_client,
                    conversation_id=conversation_id,
                    stream_message_id=uuid7_str(),
                )
                result = asyncio.run(
                    agent.run(
                        user_input,
                        conversation_id=conversation_id,
                        mode=RunMode.CHAT,
                        user_id=user_identity.user_id,
                        observer=observer,
                    )
                )
            except (KeyboardInterrupt, RunCancelled):
                agent.cancel_active_runs("interrupted")
                display.info("Run cancelled.")
                continue
            display.chat_response(result)


def _launch_app(config: OuroAgentsConfig) -> None:
    from .app import OuroApp

    OuroApp(config).run()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

