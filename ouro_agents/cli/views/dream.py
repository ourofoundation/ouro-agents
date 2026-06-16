from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Select, Static

from ..widgets.activity import ActivityLog


class DreamView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Dream[/]\n[dim]Run dream, strength decay, and memory maintenance.[/]",
            markup=True,
            classes="view-title",
        )
        yield Horizontal(
            Static("Team", classes="team-label"),
            Select([], prompt="No teams available", id="dream-team-select"),
            classes="team-row",
        )
        yield Horizontal(
            Button("Dream all teams", id="dream-all", variant="primary"),
            Button("Dream selected team", id="dream-team"),
            classes="button-row",
        )
        yield ActivityLog(id="dream-log")

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#dream-log", ActivityLog)

    @property
    def team_select(self) -> Select:
        return self.query_one("#dream-team-select", Select)

    def set_teams(
        self, options: list[tuple[str, str]], selected: str | None
    ) -> None:
        select = self.team_select
        select.set_options(options)
        values = {value for _, value in options}
        if selected in values:
            select.value = selected
