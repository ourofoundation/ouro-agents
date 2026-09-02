from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from ..widgets.activity import ActivityLog


class DreamView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Dream[/]\n[dim]Review recent runs and improve the agent's operating process.[/]",
            markup=True,
            classes="view-title",
        )
        yield Horizontal(
            Button("Run dream", id="run-dream", variant="primary"),
            classes="button-row",
        )
        yield ActivityLog(id="dream-log")

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#dream-log", ActivityLog)
