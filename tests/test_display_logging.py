from io import StringIO

from rich.console import Console

from ouro_agents.display import (
    THEME,
    OuroDisplay,
    OuroLogger,
    Verbosity,
    _NonBlockingSafeStream,
    _reasoning_for_display,
)
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


def test_off_logger_hides_records_labeled_off():
    display, buffer = _display_with_console()
    logger = OuroLogger(level=LogLevel.OFF, display=display)
    logger.log("private reasoning", level=LogLevel.OFF)
    assert buffer.getvalue() == ""


def test_reasoning_display_drops_encrypted_blobs():
    visible = "Thinking about next steps"
    encrypted = (
        visible
        + '\n{"type": "reasoning.encrypted", "data": "gAAAAA...", '
        + '"format": "openai-responses-v1"}'
    )
    assert _reasoning_for_display(encrypted) == visible
    assert _reasoning_for_display(
        '{"type": "reasoning.encrypted", "data": "gAAAAA"}'
    ) == ""


def test_reasoning_display_truncates_long_text():
    long = "x" * 5000
    out = _reasoning_for_display(long)
    assert out.endswith("… [truncated]")
    assert len(out) < 5000


def test_safe_stream_swallows_blocking_io():
    class _Blocking:
        encoding = "utf-8"

        def write(self, data):
            raise BlockingIOError(11, "write could not complete without blocking")

        def flush(self):
            raise BlockingIOError(11, "write could not complete without blocking")

        def isatty(self):
            return False

    stream = _NonBlockingSafeStream(_Blocking())
    assert stream.write("hello") == 5
    stream.flush()


def test_display_print_survives_blocking_console():
    class _BoomConsole:
        def print(self, *args, **kwargs):
            raise BlockingIOError(11, "write could not complete without blocking")

        def rule(self, *args, **kwargs):
            raise BlockingIOError(11, "write could not complete without blocking")

    display = OuroDisplay(verbosity=Verbosity.NORMAL)
    display.console = _BoomConsole()
    # Must not raise — this is what was killing chat under PM2.
    display.blank()
    display.error("boom")
    display.reasoning('hello\n{"type": "reasoning.encrypted", "data": "gAAAA"}')
    display.rule("title")


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
            "research: using web_search_exa",
            detail={"name": "research", "tool": "web_search_exa"},
        )
    )
    assert not any(
        "web_search_exa" in message for _, message in display.calls
    )
