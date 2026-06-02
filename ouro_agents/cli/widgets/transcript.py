from __future__ import annotations

from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.widgets import RichLog


class Transcript(RichLog):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("wrap", True)
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("markup", True)
        super().__init__(*args, **kwargs)

    def append_message(self, role: str, text: str, *, message_type: str = "message") -> None:
        title = role or message_type
        style = "cyan" if role in {"you", "user"} else "magenta"
        if message_type == "reasoning":
            style = "dim magenta"
        elif message_type == "tool_call":
            style = "yellow"
        self.write(Panel(Markdown(text or " "), title=title, border_style=style))

    def append_event(self, text: str, *, style: str = "dim") -> None:
        self.write(Text(text, style=style))


def message_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str) and text:
        return text
    payload = message.get("json")
    if isinstance(payload, dict):
        if isinstance(payload.get("text"), str):
            return payload["text"]
        if payload.get("name"):
            return f"Called {payload.get('name')}"
    return ""

