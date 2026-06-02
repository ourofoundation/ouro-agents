from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static


class DashboardView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]ouro-agents[/]\n"
            "[dim]Activity-oriented agent workspace backed by Ouro.[/]",
            markup=True,
            classes="view-title",
        )
        yield Static("", id="dashboard-summary")
        yield Horizontal(
            Button("New chat", id="quick-new-chat", variant="primary"),
            Button("Run task", id="quick-run"),
            Button("Heartbeat", id="quick-heartbeat"),
            Button("Quest", id="quick-quest"),
            classes="button-row",
        )
        yield Static(
            "[dim]Use the left rail or Ctrl+P command palette to move between activities.[/]",
            markup=True,
        )

    def set_summary(self, text: str) -> None:
        self.query_one("#dashboard-summary", Static).update(text)

