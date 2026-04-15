from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..modes.planning import PlanCycle


REVIEWABLE_PLAN_STATUSES = {"pending_review", "active"}


@dataclass(frozen=True)
class ReviewPlanOption:
    cycle_id: str
    title: str
    subtitle: str


def reviewable_plans(plans: list["PlanCycle"]) -> list["PlanCycle"]:
    return [plan for plan in plans if plan.status in REVIEWABLE_PLAN_STATUSES]


def build_review_plan_options(plans: list["PlanCycle"]) -> list[ReviewPlanOption]:
    options: list[ReviewPlanOption] = []
    for plan in reviewable_plans(plans):
        if plan.kind == "goal" and plan.goal:
            title = f"Goal plan: {_truncate(plan.goal, 72)}"
        else:
            title = "Default plan"

        total = len(plan.items)
        progress = (
            f"{plan.items_done}/{total} complete" if total else "no checklist items"
        )
        status = plan.status.replace("_", " ")
        quest = plan.quest_id or "no quest"
        subtitle = f"{status} | {progress} | {quest} | {plan.id[:8]}"
        options.append(
            ReviewPlanOption(
                cycle_id=plan.id,
                title=title,
                subtitle=subtitle,
            )
        )
    return options


def choose_review_plan(plans: list["PlanCycle"]) -> str | None:
    options = build_review_plan_options(plans)
    if not options:
        return None
    if len(options) == 1:
        return options[0].cycle_id
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return options[0].cycle_id

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
                self.exit(item.option.cycle_id)

        def action_cancel(self) -> None:
            self.exit(None)

    return ReviewPlanPickerApp(options).run()


def _truncate(text: str, max_length: int) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= max_length:
        return stripped
    return stripped[: max_length - 3].rstrip() + "..."
