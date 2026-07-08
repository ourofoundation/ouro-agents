from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any


REVIEWABLE_QUEST_STATUSES = {"draft", "open"}


@dataclass(frozen=True)
class ReviewPlanOption:
    quest_id: str
    title: str
    subtitle: str


def build_review_plan_options(quests: list[dict[str, Any]]) -> list[ReviewPlanOption]:
    """Build picker options from quest dicts (id, name, status, items_*)."""
    options: list[ReviewPlanOption] = []
    for quest in quests:
        if quest.get("status") not in REVIEWABLE_QUEST_STATUSES:
            continue
        title = _truncate(quest.get("name") or "Untitled quest", 72)
        total = int(quest.get("items_total") or 0)
        progress = (
            f"{quest.get('items_done', 0)}/{total} complete"
            if total
            else "no task items"
        )
        subtitle = f"{quest.get('status')} | {progress} | {quest.get('id', '')[:8]}"
        options.append(
            ReviewPlanOption(
                quest_id=str(quest.get("id") or ""),
                title=title,
                subtitle=subtitle,
            )
        )
    return options


def choose_review_plan(quests: list[dict[str, Any]]) -> str | None:
    options = build_review_plan_options(quests)
    if not options:
        return None
    if len(options) == 1:
        return options[0].quest_id
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return options[0].quest_id

    from textual.app import App, ComposeResult  # type: ignore[reportMissingImports]
    from textual.binding import Binding  # type: ignore[reportMissingImports]
    from textual.containers import Container  # type: ignore[reportMissingImports]
    from textual.widgets import Label, ListItem, ListView, Static  # type: ignore[reportMissingImports]

    class ReviewPlanItem(ListItem):
        def __init__(self, option: ReviewPlanOption) -> None:
            self.option = option
            super().__init__(
                Label(f"[b]{option.title}[/]\n[dim]{option.subtitle}[/]", markup=True)
            )

    class ReviewPlanPickerApp(App[str | None]):
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

        def __init__(self, plan_options: list[ReviewPlanOption]) -> None:
            super().__init__()
            self._plan_options = plan_options

        def compose(self) -> ComposeResult:
            yield Container(
                Static("Select A Plan To Review", id="title"),
                Static("Use arrow keys to move, Enter to review, Esc to cancel.", id="help"),
                ListView(
                    *(ReviewPlanItem(option) for option in self._plan_options),
                    id="plans",
                ),
                id="dialog",
            )

        def on_mount(self) -> None:
            list_view = self.query_one(ListView)
            list_view.index = 0
            list_view.focus()

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            item = event.item
            if isinstance(item, ReviewPlanItem):
                self.exit(item.option.quest_id)

        def action_cancel(self) -> None:
            self.exit(None)

    return ReviewPlanPickerApp(options).run()


def _truncate(text: str, max_length: int) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= max_length:
        return stripped
    return stripped[: max_length - 3].rstrip() + "..."
