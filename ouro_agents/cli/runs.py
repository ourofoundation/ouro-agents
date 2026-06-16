"""``ouro-agents runs`` — browse the SQLite run log (``runs.db``).

Read-only views over the run log written by ``ouro_agents.run_log``. These
commands open the database read-only and never start an agent.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..run_log import RunLogStore

runs_app = typer.Typer(
    name="runs",
    help="Browse the agent run log (runs.db).",
    no_args_is_help=True,
)

console = Console()

_STATUS_STYLE = {"success": "green", "error": "red", "cancelled": "yellow"}
_REL_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_REL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _open_store(ctx: typer.Context) -> RunLogStore:
    from . import _state

    config = _state(ctx).config
    path = config.run_log.path or (config.agent.workspace / "runs.db")
    return RunLogStore(Path(path), readonly=True)


def _parse_since(since: Optional[str]) -> Optional[str]:
    """Turn ``24h`` / ``7d`` / an ISO date into an absolute ISO-8601 lower bound."""
    if not since:
        return None
    match = _REL_RE.match(since)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        delta = timedelta(**{_REL_UNITS[unit]: amount})
        return (datetime.now(timezone.utc) - delta).isoformat()
    # Otherwise treat it as an absolute date/datetime the user typed verbatim.
    return since


def _short(value: Optional[str], n: int = 8) -> str:
    return (value or "")[:n]


def _preview(value: Optional[str], n: int = 60) -> str:
    text = " ".join((value or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def _status_text(status: str) -> str:
    style = _STATUS_STYLE.get(status, "white")
    return f"[{style}]{status}[/{style}]"


def _fmt_when(started_at: Optional[str]) -> str:
    if not started_at:
        return ""
    return started_at.replace("T", " ")[:19]


def _fmt_cost(cost: Optional[float]) -> str:
    return f"${cost:.4f}" if cost else "-"


@runs_app.command("list")
def list_runs(
    ctx: typer.Context,
    mode: Optional[str] = typer.Option(None, "--mode", help="Filter by mode."),
    status: Optional[str] = typer.Option(
        None, "--status", help="success | error | cancelled."
    ),
    team: Optional[str] = typer.Option(None, "--team", help="Filter by team id."),
    conversation: Optional[str] = typer.Option(
        None, "--conversation", help="Filter by conversation id."
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Relative (24h, 7d) or ISO date lower bound."
    ),
    grep: Optional[str] = typer.Option(
        None, "--grep", help="Substring match on task or result."
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows."),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """List recent runs, newest first."""
    store = _open_store(ctx)
    team_kwargs = {} if team is None else {"team_id": team, "include_shared_team": False}
    rows = store.query_runs(
        mode=mode,
        status=status,
        conversation_id=conversation,
        since=_parse_since(since),
        grep=grep,
        limit=limit,
        **team_kwargs,
    )
    store.close()

    if json_output:
        console.print_json(json.dumps(rows))
        return
    if not rows:
        console.print("[dim]No matching runs.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    for col in ("when", "run", "mode", "status", "dur", "tokens", "cost", "task"):
        table.add_column(col)
    for r in rows:
        dur = r.get("duration_s")
        table.add_row(
            _fmt_when(r.get("started_at")),
            _short(r.get("run_id")),
            r.get("mode") or "",
            _status_text(r.get("status") or ""),
            f"{dur:.1f}s" if isinstance(dur, (int, float)) else "-",
            str(r.get("total_tokens") or 0),
            _fmt_cost(r.get("cost_usd")),
            _preview(r.get("task")),
        )
    console.print(table)


@runs_app.command("show")
def show_run(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Run id (full or unique prefix)."),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    full: bool = typer.Option(
        False, "--full", help="Show full observations (default truncates)."
    ),
) -> None:
    """Show one run's full record and step trace."""
    store = _open_store(ctx)
    run = store.get_run(run_id)
    if run is None:
        # Allow a unique prefix.
        candidates = [
            row
            for row in store.query_runs(limit=10000)
            if (row.get("run_id") or "").startswith(run_id)
        ]
        if len(candidates) == 1:
            run = candidates[0]
        elif len(candidates) > 1:
            store.close()
            console.print(f"[red]Ambiguous run id prefix '{run_id}'.[/red]")
            raise typer.Exit(1)
    if run is None:
        store.close()
        console.print(f"[red]No run found for '{run_id}'.[/red]")
        raise typer.Exit(1)

    steps = store.get_run_steps(run["run_id"])
    store.close()

    if json_output:
        console.print_json(json.dumps({"run": run, "steps": steps}))
        return

    meta = [
        f"[bold]mode[/bold] {run.get('mode')}    "
        f"[bold]status[/bold] {_status_text(run.get('status') or '')}",
        f"[bold]started[/bold] {_fmt_when(run.get('started_at'))}    "
        f"[bold]duration[/bold] {run.get('duration_s')}s",
        f"[bold]model[/bold] {run.get('model')}",
        f"[bold]tokens[/bold] {run.get('total_tokens')}    "
        f"[bold]cost[/bold] {_fmt_cost(run.get('cost_usd'))}    "
        f"[bold]steps[/bold] {run.get('num_steps')}",
    ]
    for label in ("team_id", "conversation_id", "user_id", "event_type", "tick_id",
                  "parent_run_id"):
        if run.get(label):
            meta.append(f"[bold]{label}[/bold] {run.get(label)}")
    if run.get("preflight_intent"):
        meta.append(
            f"[bold]preflight[/bold] {run.get('preflight_intent')} "
            f"/ {run.get('preflight_complexity')}"
        )
    if run.get("error_message"):
        meta.append(f"[red]error[/red] {run.get('error_message')}")
    console.print(Panel("\n".join(meta), title=run["run_id"], expand=False))

    console.print("\n[bold]task[/bold]")
    console.print(run.get("task") or "")
    console.print("\n[bold]result[/bold]")
    console.print(run.get("result") or "")

    if steps:
        console.print("\n[bold]steps[/bold]")
        for s in steps:
            head = f"  [{s.get('step_index')}] {s.get('step_type')}"
            if s.get("step_number") is not None:
                head += f" (step {s.get('step_number')})"
            console.print(head, style="cyan")
            if s.get("model_output"):
                console.print(f"      {_preview(s['model_output'], 200)}")
            if s.get("tool_calls_json"):
                try:
                    for tc in json.loads(s["tool_calls_json"]):
                        console.print(
                            f"      → {tc.get('name')}({json.dumps(tc.get('args', {}))[:160]})",
                            style="magenta",
                        )
                except Exception:
                    pass
            obs = s.get("observations")
            if obs:
                obs = obs if full else _preview(obs, 200)
                console.print(f"      ⤷ {obs}", style="dim")
            if s.get("error"):
                console.print(f"      error: {s['error']}", style="red")


@runs_app.command("stats")
def stats(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(
        None, "--since", help="Relative (24h, 7d) or ISO date lower bound."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Aggregate counts, cost, tokens, and failures by mode."""
    store = _open_store(ctx)
    rows = store.stats_by_mode(since=_parse_since(since))
    store.close()

    if json_output:
        console.print_json(json.dumps(rows))
        return
    if not rows:
        console.print("[dim]No runs recorded yet.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    for col in ("mode", "runs", "failures", "tokens", "cost"):
        table.add_column(col)
    total_runs = total_fail = total_tokens = 0
    total_cost = 0.0
    for r in rows:
        total_runs += r["runs"]
        total_fail += r["failures"]
        total_tokens += int(r["total_tokens"] or 0)
        total_cost += float(r["cost_usd"] or 0)
        table.add_row(
            r["mode"] or "",
            str(r["runs"]),
            str(r["failures"]),
            str(int(r["total_tokens"] or 0)),
            _fmt_cost(r["cost_usd"]),
        )
    table.add_section()
    table.add_row(
        "[bold]total[/bold]",
        str(total_runs),
        str(total_fail),
        str(total_tokens),
        _fmt_cost(total_cost),
    )
    console.print(table)
