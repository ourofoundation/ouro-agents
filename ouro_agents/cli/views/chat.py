from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from ..conversations import ConversationSummary
from ..widgets.transcript import Transcript


class ConversationItem(ListItem):
    def __init__(self, conversation: ConversationSummary) -> None:
        self.conversation = conversation
        label = f"[b]{conversation.name}[/]\n[dim]{conversation.id}[/]"
        super().__init__(Label(label, markup=True))


class ChatSidebar(Vertical):
    """Contextual sidebar shown in place of the global nav while in Chat."""

    def compose(self) -> ComposeResult:
        yield Button("\u2039 Back", id="nav-back")
        yield Static("[b]Chats[/]", markup=True)
        yield Horizontal(
            Button("New chat", id="new-chat", variant="primary"),
            Button("Refresh", id="refresh-chats"),
            id="chat-actions",
        )
        yield ListView(id="conversation-list")

    @property
    def conversations(self) -> ListView:
        return self.query_one("#conversation-list", ListView)

    def set_conversations(self, conversations: list[ConversationSummary]) -> None:
        list_view = self.conversations
        list_view.clear()
        for conversation in conversations:
            list_view.append(ConversationItem(conversation))


class ChatView(Vertical):
    def compose(self) -> ComposeResult:
        yield Transcript(id="chat-transcript")
        yield Input(placeholder="Message the agent...", id="chat-input")

    @property
    def transcript(self) -> Transcript:
        return self.query_one("#chat-transcript", Transcript)

    @property
    def input(self) -> Input:
        return self.query_one("#chat-input", Input)

    def clear_transcript(self) -> None:
        self.transcript.clear()
