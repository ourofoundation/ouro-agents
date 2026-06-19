from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..cli.memory import MemoryFilters
    from ..memory import MemoryBackend, MemoryResult


def clamp_index(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return max(0, min(index, count - 1))


def index_after_delete(index: int, remaining: int) -> int:
    if remaining <= 0:
        return 0
    return min(index, remaining - 1)


def run_memory_browser(
    backend: "MemoryBackend",
    agent_id: str,
    filters: "MemoryFilters",
    memories: list["MemoryResult"],
) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Memory browser requires a TTY")
    _browser_app_class()(backend, agent_id, filters, memories).run()


def _browser_app_class():
    from textual.app import App, ComposeResult  # type: ignore[reportMissingImports]
    from textual.binding import Binding  # type: ignore[reportMissingImports]
    from textual.containers import Vertical  # type: ignore[reportMissingImports]
    from textual.screen import ModalScreen  # type: ignore[reportMissingImports]
    from textual.widgets import Footer, Header, Static, TextArea  # type: ignore[reportMissingImports]

    from ..cli.memory import MemoryFilters, fetch_memories, format_memory_meta

    class EditMemoryScreen(ModalScreen[Optional[str]]):
        BINDINGS = [
            Binding("escape", "cancel", "Cancel"),
            Binding("ctrl+s", "save", "Save"),
        ]

        CSS = """
        EditMemoryScreen {
            align: center middle;
        }

        #edit-dialog {
            width: 90%;
            max-width: 100;
            height: 70%;
            border: round $accent;
            padding: 1 2;
            background: $surface;
        }

        #edit-title {
            text-style: bold;
            margin-bottom: 1;
        }

        #edit-area {
            height: 1fr;
            margin: 1 0;
        }

        #edit-help {
            color: $text-muted;
        }
        """

        def __init__(self, text: str) -> None:
            super().__init__()
            self._initial_text = text

        def compose(self) -> ComposeResult:
            yield Vertical(
                Static("Edit memory", id="edit-title"),
                TextArea(self._initial_text, id="edit-area"),
                Static("Ctrl+S save · Esc cancel", id="edit-help"),
                id="edit-dialog",
            )

        def on_mount(self) -> None:
            self.query_one(TextArea).focus()

        def action_save(self) -> None:
            text = self.query_one(TextArea).text.strip()
            self.dismiss(text or None)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class MemoryBrowserApp(App[None]):
        BINDINGS = [
            Binding("left", "previous", "Previous"),
            Binding("right", "next", "Next"),
            Binding("up", "previous", "Previous", show=False),
            Binding("down", "next", "Next", show=False),
            Binding("d", "delete", "Delete"),
            Binding("e", "edit", "Edit"),
            Binding("q", "quit", "Quit"),
            Binding("escape", "quit", "Quit", show=False),
        ]

        CSS = """
        Screen {
            layout: vertical;
        }

        #meta {
            height: auto;
            max-height: 8;
            padding: 0 1;
            color: $text-muted;
        }

        #body {
            height: 1fr;
            padding: 1 2;
            border: round $primary-darken-2;
            margin: 0 1;
            overflow-y: auto;
        }

        #status {
            height: auto;
            padding: 0 1;
            color: $warning;
        }
        """

        def __init__(
            self,
            backend,
            agent_id: str,
            filters: MemoryFilters,
            memories: list,
        ) -> None:
            super().__init__()
            self.backend = backend
            self.agent_id = agent_id
            self.filters = filters
            self.memories = list(memories)
            self.index = 0
            self._pending_delete = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static("", id="meta")
            yield Static("", id="body")
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            self._render()

        @property
        def current(self):
            if not self.memories:
                return None
            return self.memories[clamp_index(self.index, len(self.memories))]

        def _render(self) -> None:
            meta = self.query_one("#meta", Static)
            body = self.query_one("#body", Static)
            status = self.query_one("#status", Static)

            if not self.memories:
                meta.update(
                    f"[bold]Memory curation[/bold] — {self.agent_id} · "
                    f"[dim]{self.filters.summary()}[/dim]"
                )
                body.update("[dim]No matching memories. Press q to quit.[/dim]")
                status.update("")
                self.sub_title = "empty"
                return

            self.index = clamp_index(self.index, len(self.memories))
            memory = self.current
            assert memory is not None
            position = f"{self.index + 1}/{len(self.memories)}"
            self.title = "Memory curation"
            self.sub_title = f"{self.agent_id} · {position} · weakest first"

            meta.update(format_memory_meta(memory, position=position, filters=self.filters))
            body.update(memory.text)
            if self._pending_delete:
                status.update("[bold yellow]Press d again to delete[/bold yellow]")
            else:
                status.update("")

        def _clear_pending(self) -> None:
            if self._pending_delete:
                self._pending_delete = False

        def action_previous(self) -> None:
            self._clear_pending()
            if not self.memories or self.index <= 0:
                return
            self.index -= 1
            self._render()

        def action_next(self) -> None:
            self._clear_pending()
            if not self.memories or self.index >= len(self.memories) - 1:
                return
            self.index += 1
            self._render()

        def action_delete(self) -> None:
            if not self.memories:
                return
            if not self._pending_delete:
                self._pending_delete = True
                self._render()
                return
            self.action_confirm_delete()

        def action_confirm_delete(self) -> None:
            if not self._pending_delete:
                return
            memory = self.current
            if memory is None:
                return
            self.backend.delete(memory.id)
            self.memories.pop(self.index)
            self._pending_delete = False
            self.index = index_after_delete(self.index, len(self.memories))
            self._render()

        def action_edit(self) -> None:
            memory = self.current
            if memory is None or self._pending_delete:
                return

            def apply_edit(new_text: Optional[str]) -> None:
                if not new_text or new_text == memory.text:
                    return
                try:
                    self.backend.update_text(memory.id, new_text)
                except Exception as exc:
                    self.notify(f"Update failed: {exc}", severity="error")
                    return
                memory.text = new_text
                self._render()
                self.notify("Memory updated", severity="information")

            self.push_screen(EditMemoryScreen(memory.text), apply_edit)

        def action_quit(self) -> None:
            self.exit()

    return MemoryBrowserApp
