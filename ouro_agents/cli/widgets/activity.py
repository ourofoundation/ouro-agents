from __future__ import annotations

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.widgets import RichLog, Static


class ActivityLog(RichLog):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("wrap", True)
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("markup", True)
        super().__init__(*args, **kwargs)

    def line(self, text: str, *, style: str = "dim") -> None:
        self.write(Text(text, style=style))

    def markdown(self, text: str) -> None:
        if text:
            self.write(Markdown(text))

    def panel(self, title: str, text: str) -> None:
        self.write(Panel(Markdown(text or " "), title=title, expand=False))


class StatusBar(Static):
    def set_status(
        self,
        *,
        view: str,
        agent_name: str,
        model: str,
        user: str = "",
        agent: str = "",
        usage: str = "",
    ) -> None:
        identities = " | ".join(
            part
            for part in [
                f"you: {user}" if user else "",
                f"agent: {agent}" if agent else "",
            ]
            if part
        )
        parts = [
            f"[b]{view}[/]",
            f"{agent_name} ({model})",
            identities,
            usage,
        ]
        self.update("   ".join(part for part in parts if part))

