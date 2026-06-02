from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Static

from ..widgets.activity import ActivityLog


class RunsView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Runs[/]\n[dim]Trigger general autonomous agent execution.[/]",
            markup=True,
            classes="view-title",
        )
        yield Horizontal(
            Input(placeholder="What should the agent do?", id="run-input"),
            Button("Run", id="start-run", variant="primary"),
            classes="input-row",
        )
        yield ActivityLog(id="runs-log")

    @property
    def input(self) -> Input:
        return self.query_one("#run-input", Input)

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#runs-log", ActivityLog)

