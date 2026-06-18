from ouro_agents.artifacts import PrefetchSpec
from ouro_agents.cli_progress import TerminalRunProgress
from ouro_agents.config import RunMode
from ouro_agents.display import Verbosity
from ouro_agents.events import EventRunContext
from ouro_agents.observer import AgentObserver, CompositeAgentObserver, ProgressEvent


class _RecordingObserver(AgentObserver):
    def __init__(self):
        self.events: list[tuple] = []

    def on_activity(self, status: str, message: str | None, active: bool) -> None:
        self.events.append(("activity", status, message, active))

    def on_stream_chunk(self, chunk: str) -> None:
        self.events.append(("stream", chunk))

    def on_result_ready(self, result_text: str) -> None:
        self.events.append(("result", result_text))

    def on_progress(self, event: ProgressEvent) -> None:
        self.events.append(("progress", event.phase, event.message, event.state))


class _FailingObserver(AgentObserver):
    def on_activity(self, status: str, message: str | None, active: bool) -> None:
        raise RuntimeError("boom")


class _FakeConsole:
    is_terminal = False


class _FakeDisplay:
    verbosity = Verbosity.NORMAL
    console = _FakeConsole()

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def blank(self) -> None:
        self.calls.append(("blank", ""))

    def rule(self, title: str = "") -> None:
        self.calls.append(("rule", title))

    def info(self, message: str) -> None:
        self.calls.append(("info", message))

    def success(self, message: str) -> None:
        self.calls.append(("success", message))

    def error(self, message: str) -> None:
        self.calls.append(("error", message))


def test_composite_observer_fans_out_and_isolates_failures():
    first = _RecordingObserver()
    second = _RecordingObserver()
    composite = CompositeAgentObserver(_FailingObserver(), first, second)

    composite.on_activity("thinking", "is working", True)
    composite.on_stream_chunk("hello")
    composite.on_result_ready("done")
    composite.on_progress(ProgressEvent("running_agent", "looping"))

    assert first.events == [
        ("activity", "thinking", "is working", True),
        ("stream", "hello"),
        ("result", "done"),
        ("progress", "running_agent", "looping", "active"),
    ]
    assert second.events == first.events


def test_terminal_progress_summarizes_event_context():
    display = _FakeDisplay()
    event_run = EventRunContext(
        event_type="mention",
        task="reply",
        mode=RunMode.CHAT,
        conversation_id="019eabf4-7f32-700b-8edf-76c4887becd9",
        user_id="user-1",
        preload_tools=("ouro:get_asset", "ouro:create_comment"),
        prefetch=PrefetchSpec(
            asset_ids=["019eabf4-aaaa-bbbb-cccc-76c4887becd9"],
            comment_parent_ids=["root-comment"],
            thread_comment_parent_ids=["thread-comment"],
            focus_comment_id="019eabf4-focus-comment",
        ),
        root_asset_id="019eabf4-7f32-700b-8edf-76c4887becd9",
        root_asset_type="post",
        actor_username="mmoderwell",
        team_id="019d08f9-2894-7f90-b8bc-cbde6a8c5896",
    )
    progress = TerminalRunProgress(event_run, display)

    progress.start()
    progress.on_activity("thinking", "is analyzing the task...", True)
    progress.on_progress(
        ProgressEvent(
            "subagent_started",
            "research",
            detail={"name": "research", "max_steps": 20},
        )
    )
    progress.on_progress(
        ProgressEvent(
            "subagent_completed",
            "research",
            state="complete",
            detail={
                "name": "research",
                "usage": {"steps": 8, "total_tokens": 270258, "cost_usd": 0.023119},
                "asset": "post:019eabf7...",
            },
        )
    )
    progress.finish("NO_ACTION")

    assert ("rule", "mention event") in display.calls
    assert (
        "info",
        "trigger: @mmoderwell on post:019eabf4...ecd9",
    ) in display.calls
    assert ("info", "ready tools: get_asset, create_comment") in display.calls
    assert (
        "info",
        "prefetch: 1 asset, 1 comment thread, 1 reply thread, focus=019eabf4...ment",
    ) in display.calls
    assert ("info", "> subagent: research (max_steps=20)") in display.calls
    assert any(
        kind == "success"
        and message.startswith(
            "ok subagent: research | 8 steps | 270,258 tok | $0.023119 | post:019eabf7..."
        )
        for kind, message in display.calls
    )
    assert any(
        kind == "success" and message.startswith("ok complete: run no action")
        for kind, message in display.calls
    )


def test_token_progress_includes_current_context_size():
    display = _FakeDisplay()
    event_run = EventRunContext(
        event_type="mention",
        task="reply",
        mode=RunMode.CHAT,
        conversation_id="conversation-1",
        user_id="user-1",
    )
    progress = TerminalRunProgress(event_run, display)

    label = progress._token_label(
        ProgressEvent(
            "token_update",
            detail={
                "total_tokens": 1_250,
                "current_context_tokens": 900,
                "input_tokens": 1_000,
                "output_tokens": 250,
                "cost_usd": 0.01,
            },
        )
    )

    assert label == "1,250 tok | ctx 900 | in 1,000 | out 250 | $0.010000"
