from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, Static

from ..widgets.activity import ActivityLog


def _quest_progress(quest: dict[str, Any]) -> str:
    total = int(quest.get("items_total") or 0)
    if not total:
        return "no items"
    return f"{quest.get('items_resolved', 0)}/{total}"


class QuestItem(ListItem):
    def __init__(self, quest: dict[str, Any], *, team_label: str = "") -> None:
        self.quest = quest
        title = quest.get("name") or "Untitled quest"
        subtitle_parts = [str(quest.get("status") or ""), _quest_progress(quest)]
        if team_label:
            subtitle_parts.append(team_label)
        subtitle = " · ".join(part for part in subtitle_parts if part)
        super().__init__(Label(f"[b]{title}[/]\n[dim]{subtitle}[/]", markup=True))


class QuestSidebar(Vertical):
    """Contextual sidebar shown in place of the global nav while in Quests."""

    def compose(self) -> ComposeResult:
        yield Button("\u2039 Back", id="nav-back")
        yield Static("[b]Quests[/]", markup=True)
        yield Horizontal(
            Button("New quest", id="new-quest", variant="primary"),
            Button("Refresh", id="refresh-quests"),
            id="quest-actions",
        )
        yield ListView(id="quest-list")

    @property
    def quests(self) -> ListView:
        return self.query_one("#quest-list", ListView)

    def set_quests(self, items: list[QuestItem]) -> None:
        list_view = self.quests
        list_view.clear()
        for item in items:
            list_view.append(item)


class QuestsView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Quests[/]\n[dim]Create and track quests by team. "
            "Pick a quest in the sidebar to see its items.[/]",
            markup=True,
            classes="view-title",
        )
        yield Horizontal(
            Static("Team", classes="team-label"),
            Select([], prompt="No teams available", id="quest-team-select"),
            classes="team-row",
        )
        yield Horizontal(
            Input(placeholder="Optional quest goal/directive", id="quest-input"),
            Button("Create quest", id="create-quest", variant="primary"),
            classes="input-row",
        )
        yield ActivityLog(id="quests-log")

    @property
    def input(self) -> Input:
        return self.query_one("#quest-input", Input)

    @property
    def log(self) -> ActivityLog:
        return self.query_one("#quests-log", ActivityLog)

    @property
    def team_select(self) -> Select:
        return self.query_one("#quest-team-select", Select)

    def set_teams(
        self, options: list[tuple[str, str]], selected: str | None
    ) -> None:
        select = self.team_select
        select.set_options(options)
        values = {value for _, value in options}
        if selected in values:
            select.value = selected
