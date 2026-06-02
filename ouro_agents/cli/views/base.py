from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from ..widgets.activity import ActivityLog


class ActivityView(Vertical):
    title = "View"
    description = ""

    def compose(self) -> ComposeResult:
        yield Static(
            f"[b]{self.title}[/]\n[dim]{self.description}[/]",
            markup=True,
            classes="view-title",
        )
        yield ActivityLog(id=f"{self.id or self.__class__.__name__}-log")

    @property
    def log(self) -> ActivityLog:
        return self.query_one(ActivityLog)

