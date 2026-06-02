from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import Label, ListItem


@dataclass(frozen=True)
class NavTarget:
    key: str
    title: str
    subtitle: str = ""


class NavItem(ListItem):
    def __init__(self, target: NavTarget) -> None:
        self.target = target
        label = f"[b]{target.title}[/]"
        if target.subtitle:
            label += f"\n[dim]{target.subtitle}[/]"
        super().__init__(Label(label, markup=True))

