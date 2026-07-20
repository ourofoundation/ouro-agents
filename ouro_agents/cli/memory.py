"""``ouro-agents memory`` — interactive curation of the agent's semantic memory.

One entry point: browse memories one at a time (weakest first), navigate with
arrow keys, delete or edit with keyboard shortcuts. Talks to the configured
memory backend directly and never starts an agent run, so viewing does not
reinforce or otherwise mutate memories.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console

from ..constants import parse_since_datetime
from ..memory import MemoryResult, create_memory_backend

console = Console()


@dataclass
class MemoryFilters:
    team: Optional[str] = None
    category: Optional[str] = None
    subject_type: Optional[str] = None
    since: Optional[datetime] = None
    grep: Optional[str] = None
    limit: int = 50

    def summary(self) -> str:
        parts: list[str] = []
        if self.team:
            parts.append(f"team={self.team}")
        if self.category:
            parts.append(f"category={self.category}")
        if self.subject_type:
            parts.append(f"subject={self.subject_type}")
        if self.since:
            parts.append(f"since={self.since.date().isoformat()}")
        if self.grep:
            parts.append(f"grep={self.grep!r}")
        parts.append(f"limit={self.limit}")
        return ", ".join(parts) if parts else "no filters"


def _backend_and_agent(ctx: typer.Context):
    from . import _state

    config = _state(ctx).config
    backend = create_memory_backend(config.memory)
    return backend, config.agent.name


def _parse_since(since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    try:
        return parse_since_datetime(since)
    except ValueError as exc:
        raise typer.BadParameter(f"Could not parse --since '{since}'.") from exc


def _age(created_at: str) -> str:
    if not created_at:
        return "-"
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return "-"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - created
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _subject(m: MemoryResult) -> str:
    if m.subject_id:
        return f"{m.subject_type}:{m.subject_id[:8]}"
    return m.subject_type or "-"


def _as_dict(m: MemoryResult) -> dict:
    return {
        "id": m.id,
        "text": m.text,
        "category": m.category,
        "strength": m.strength,
        "subject_type": m.subject_type,
        "subject_id": m.subject_id,
        "team_id": m.team_id,
        "team_ids": m.team_ids,
        "asset_ids": m.asset_ids,
        "basis": m.basis,
        "stability": m.stability,
        "created_at": m.created_at,
        "last_accessed": m.last_accessed,
    }


def format_memory_meta(
    memory: MemoryResult,
    *,
    position: str,
    filters: MemoryFilters,
) -> str:
    lines = [
        f"[bold]{position}[/bold] · [cyan]{memory.category}[/cyan] · "
        f"strength {memory.strength:.2f} · age {_age(memory.created_at)}",
        f"subject {_subject(memory)} · basis {memory.basis} · stability {memory.stability}",
        f"id {memory.id}",
    ]
    if memory.team_ids:
        lines.append(f"teams {', '.join(memory.team_ids)}")
    if memory.asset_ids:
        lines.append(f"assets {', '.join(memory.asset_ids)}")
    lines.append(f"[dim]{filters.summary()}[/dim]")
    return "\n".join(lines)


def fetch_memories(backend, agent_id: str, filters: MemoryFilters) -> list[MemoryResult]:
    memories = backend.get_all(
        agent_id=agent_id,
        limit=max(filters.limit, 1) if filters.grep is None else 10000,
        team_id=filters.team,
        category=filters.category,
        subject_type=filters.subject_type,
        since=filters.since,
    )
    if filters.grep:
        needle = filters.grep.lower()
        memories = [m for m in memories if needle in m.text.lower()]
    memories.sort(key=lambda m: m.strength)
    return memories[: max(filters.limit, 1)]


def register(cli: typer.Typer) -> None:
    @cli.command(
        "memory",
        help="Interactively inspect and curate the agent's semantic memory.",
    )
    def memory(
        ctx: typer.Context,
        team: Optional[str] = typer.Option(None, "--team", help="Initial team filter."),
        category: Optional[str] = typer.Option(
            None, "--category", help="Initial category: fact | preference | direction."
        ),
        subject_type: Optional[str] = typer.Option(
            None, "--subject-type", help="Initial subject filter."
        ),
        since: Optional[str] = typer.Option(
            None, "--since", help="Initial time filter: 24h, 7d, or ISO date."
        ),
        grep: Optional[str] = typer.Option(
            None, "--grep", help="Initial substring filter on memory text."
        ),
        limit: int = typer.Option(50, "--limit", "-n", help="Max memories to load."),
        json_output: bool = typer.Option(
            False, "--json", help="Dump matching memories as JSON and exit."
        ),
    ) -> None:
        """Browse, inspect, edit, and delete agent memories interactively."""
        backend, agent_id = _backend_and_agent(ctx)
        filters = MemoryFilters(
            team=team,
            category=category,
            subject_type=subject_type,
            since=_parse_since(since),
            grep=grep,
            limit=max(limit, 1),
        )
        memories = fetch_memories(backend, agent_id, filters)

        if json_output:
            console.print_json(json.dumps([_as_dict(m) for m in memories]))
            return

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            console.print(
                "[red]Interactive memory curation requires a TTY. "
                "Use --json for scripted output.[/red]"
            )
            raise typer.Exit(1)

        from ..tui.memory_browser import run_memory_browser

        run_memory_browser(backend, agent_id, filters, memories)
