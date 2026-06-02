from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static

from ..widgets.activity import ActivityLog


class ReviewView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Review[/]\n"
            "[dim]Run a standalone review heartbeat for pending or active quests.[/]",
            markup=True,
            classes="view-title",
        )
        yield Button("Review current quest", id="run-review", variant="primary")
        yield ActivityLog(id="review-log")

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#review-log", ActivityLog)

