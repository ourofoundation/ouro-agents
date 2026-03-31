"""Smolagents tweaks for Ouro console output.

ToolCallingAgent's streaming Live view calls ChatMessage.render_as_markdown(), which
by default appends one JSON line per tool call. OuroLogger already prints a compact
``> tool_name(args)`` line when the tool runs, so the JSON duplicates noise.
"""

from smolagents.models import ChatMessage


def _render_as_markdown_ouro(self: ChatMessage) -> str:
    return str(self.content) or ""


def apply() -> None:
    ChatMessage.render_as_markdown = _render_as_markdown_ouro  # type: ignore[method-assign]


apply()
