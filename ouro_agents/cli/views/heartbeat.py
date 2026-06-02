from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from ..widgets.activity import ActivityLog


class HeartbeatView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Heartbeat[/]\n"
            "[dim]Run one proactive tick now, or review the configured cadence.[/]",
            markup=True,
            classes="view-title",
        )
        yield Static("", id="heartbeat-status", classes="status-panel")
        yield Horizontal(
            Button("Trigger heartbeat", id="trigger-heartbeat", variant="primary"),
            Button("Refresh", id="refresh-heartbeat"),
            classes="button-row",
        )
        yield ActivityLog(id="heartbeat-log")

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#heartbeat-log", ActivityLog)

    def set_status(self, text: str) -> None:
        self.query_one("#heartbeat-status", Static).update(text)
