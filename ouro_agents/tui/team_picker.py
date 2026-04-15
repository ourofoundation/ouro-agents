from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..teams import TeamInfo


@dataclass(frozen=True)
class TeamOption:
    team_id: str
    title: str
    subtitle: str


def build_team_options(teams: list["TeamInfo"]) -> list[TeamOption]:
    options: list[TeamOption] = []
    sorted_teams = sorted(
        teams,
        key=lambda team: (
            (team.name or "").lower(),
            (team.slug or "").lower(),
            team.id,
        ),
    )
    for team in sorted_teams:
        title = team.name or team.slug or team.id
        details: list[str] = []
        if team.slug and team.slug != title:
            details.append(team.slug)
        if team.org_id:
            details.append(team.org_id)
        details.append(team.id)
        options.append(
            TeamOption(
                team_id=team.id,
                title=title,
                subtitle=" | ".join(details),
            )
        )
    return options


def choose_plan_team(teams: list["TeamInfo"]) -> str | None:
    options = build_team_options(teams)
    if not options:
        return None
    if len(options) == 1:
        return options[0].team_id
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return options[0].team_id

    from textual.app import App, ComposeResult  # type: ignore[reportMissingImports]
    from textual.binding import Binding  # type: ignore[reportMissingImports]
    from textual.containers import Container  # type: ignore[reportMissingImports]
    from textual.widgets import Label, ListItem, ListView, Static  # type: ignore[reportMissingImports]

    class TeamItem(ListItem):
        def __init__(self, option: TeamOption) -> None:
            self.option = option
            super().__init__(
                Label(f"[b]{option.title}[/]\n[dim]{option.subtitle}[/]", markup=True)
            )

    class TeamPickerApp(App[str | None]):
        CSS = """
        Screen {
            align: center middle;
        }

        #dialog {
            width: 88;
            max-height: 24;
            border: round $accent;
            padding: 1 2;
            background: $surface;
        }

        #title {
            text-style: bold;
            margin-bottom: 1;
        }

        #help {
            color: $text-muted;
            margin-bottom: 1;
        }

        ListView {
            height: auto;
            max-height: 16;
        }

        ListItem {
            padding: 0 1;
        }
        """

        BINDINGS = [
            Binding("escape", "cancel", "Cancel"),
            Binding("q", "cancel", "Cancel", show=False),
        ]

        def __init__(self, team_options: list[TeamOption]) -> None:
            super().__init__()
            self._team_options = team_options

        def compose(self) -> ComposeResult:
            yield Container(
                Static("Select A Team For The Plan", id="title"),
                Static("Use arrow keys to move, Enter to continue, Esc to cancel.", id="help"),
                ListView(*(TeamItem(option) for option in self._team_options), id="teams"),
                id="dialog",
            )

        def on_mount(self) -> None:
            list_view = self.query_one(ListView)
            list_view.index = 0
            list_view.focus()

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            item = event.item
            if isinstance(item, TeamItem):
                self.exit(item.option.team_id)

        def action_cancel(self) -> None:
            self.exit(None)

    return TeamPickerApp(options).run()
