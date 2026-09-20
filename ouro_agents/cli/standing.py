"""``ouro-agents standing`` — operator access to the STANDING directives.

STANDING is the small, always-loaded list of cross-team directives that bind
every run (see ``memory.standing``). Agents maintain it through the reflector
and the ``standing_set`` / ``standing_clear`` tools; this command gives the
operator the same three verbs without starting an agent run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()

standing_app = typer.Typer(
    name="standing",
    help="List, set, or clear the agent's cross-team STANDING directives.",
    no_args_is_help=True,
)


def _store(ctx: typer.Context):
    from . import _state
    from ..memory.ouro_docs import LocalDocStore

    config = _state(ctx).config
    return LocalDocStore(Path(config.agent.workspace), agent_name=config.agent.name)


@standing_app.command("list")
def list_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show the current STANDING entries and whether each is overdue."""
    from ..memory.standing import load_standing

    doc = load_standing(_store(ctx))
    if json_output:
        console.print_json(
            json.dumps(
                [
                    {
                        "id": e.id,
                        "since": e.since,
                        "source": e.source,
                        "until": e.until,
                        "overdue": e.is_overdue(),
                        "text": e.text,
                    }
                    for e in doc.entries
                ]
            )
        )
        return
    if not doc.entries:
        console.print("[dim]STANDING is empty.[/dim]")
        return
    for entry in doc.entries:
        flag = " [yellow](overdue)[/yellow]" if entry.is_overdue() else ""
        console.print(
            f"[bold]{entry.id}[/bold]  since {entry.since} · from {entry.source}"
            + (f" · until {entry.until}" if entry.until else "")
            + flag
        )
        console.print(f"    {entry.text}")


@standing_app.command("set")
def set_cmd(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="The directive, one or two sentences."),
    until: str = typer.Option(
        ..., "--until", help="ISO date/time or a named condition with a pointer."
    ),
    source: Optional[str] = typer.Option(
        None, "--from", help="Who set it, e.g. @mmoderwell (default: operator)."
    ),
) -> None:
    """Add a directive, or refresh a near-duplicate already present."""
    from ..memory.standing import set_standing

    entry, error = set_standing(
        _store(ctx), text, source=source or "operator", until=until
    )
    if entry is None:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]set[/green] {entry.id}: {entry.text}")


@standing_app.command("clear")
def clear_cmd(
    ctx: typer.Context,
    entry_id: str = typer.Argument(..., help="Six-character entry id."),
) -> None:
    """Remove a directive whose condition has been met."""
    from ..memory.standing import clear_standing

    entry, error = clear_standing(_store(ctx), entry_id)
    if entry is None:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]cleared[/green] {entry.id}: {entry.text}")
