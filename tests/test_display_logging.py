from io import StringIO

from rich.console import Console

from ouro_agents.display import THEME, OuroDisplay, OuroLogger, Verbosity
from smolagents.monitoring import LogLevel


def _display_with_console() -> tuple[OuroDisplay, StringIO]:
    buffer = StringIO()
    display = OuroDisplay(verbosity=Verbosity.NORMAL)
    display.console = Console(
        file=buffer,
        force_terminal=False,
        width=120,
        theme=THEME,
    )
    return display, buffer


def test_tool_call_completes_in_place():
    display, buffer = _display_with_console()
    display.begin_tool_call("delegate")
    display.complete_tool_call("delegate")
    output = buffer.getvalue()
    assert output.count("delegate") == 2
    assert ">" in output.splitlines()[0]
    assert "✓" in output.splitlines()[-1]


def test_observation_renders_in_dim_style():
    display, buffer = _display_with_console()
    display.observation('{"status": "ok"}')
    output = buffer.getvalue()
    assert "Observation:" in output
    assert '{"status": "ok"}' in output


def test_logger_completes_tool_after_observation():
    display, buffer = _display_with_console()
    logger = OuroLogger(level=LogLevel.INFO, display=display)
    logger.log("Observations: tool result")
    output = buffer.getvalue()
    assert "✓" not in output
    logger._last_tool_name = "get_asset"
    logger.log("Observations: asset payload")
    output = buffer.getvalue()
    assert "Observation:" in output
    assert "asset payload" in output
    assert "✓ get_asset" in output.replace("[/]", "")


def test_subagent_step_is_spinner_only():
    from ouro_agents.cli_progress import TerminalRunProgress
    from ouro_agents.events import EventRunContext
    from ouro_agents.config import RunMode
    from ouro_agents.observer import ProgressEvent

    class _FakeDisplay:
        verbosity = Verbosity.NORMAL

        class _Console:
            is_terminal = False

        console = _Console()
        calls: list[tuple[str, str]] = []

        def blank(self) -> None:
            pass

        def rule(self, title: str = "") -> None:
            pass

        def info(self, message: str) -> None:
            self.calls.append(("info", message))

        def success(self, message: str) -> None:
            self.calls.append(("success", message))

        def error(self, message: str) -> None:
            self.calls.append(("error", message))

    display = _FakeDisplay()
    event_run = EventRunContext(
        event_type="mention",
        task="reply",
        mode=RunMode.CHAT,
        conversation_id="conversation-1",
        user_id="user-1",
    )
    progress = TerminalRunProgress(event_run, display)
    progress.on_progress(
        ProgressEvent(
            "subagent_step",
            "research: using tavily_search",
            detail={"name": "research", "tool": "tavily_search"},
        )
    )
    assert not any(
        "tavily_search" in message for _, message in display.calls
    )
