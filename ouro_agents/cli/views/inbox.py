from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static

from ..widgets.activity import ActivityLog


class InboxView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Inbox[/]\n[dim]Assigned quests, mentions, and work surfaced from Ouro.[/]",
            markup=True,
            classes="view-title",
        )
        yield Button("Refresh inbox", id="refresh-inbox", variant="primary")
        yield ActivityLog(id="inbox-log")

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#inbox-log", ActivityLog)

