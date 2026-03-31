"""Step-callback construction for agent runs."""

import logging
from typing import Callable, Optional

from smolagents import ActionStep

from ..display import OuroDisplay, get_display
from ..usage import UsageTracker

logger = logging.getLogger(__name__)

RunStatusCallback = Callable[[str, Optional[str], bool], None]


def tool_activity_message(tool_name: str) -> str:
    if tool_name == "load_tool":
        return "is preparing a tool"
    if tool_name == "delegate":
        return "is delegating to a subagent"
    if tool_name.startswith("memory_"):
        return "is checking memory"
    if tool_name in ("python_interpreter", "run_python"):
        return "is running Python"
    return f"is using {tool_name}"


def build_step_callback(
    tracker: UsageTracker,
    status_callback: Optional[RunStatusCallback] = None,
    display: Optional[OuroDisplay] = None,
) -> Callable[[ActionStep], None]:
    last_message: dict[str, Optional[str]] = {"value": None}
    _display = display or get_display()

    def _emit(message: str) -> None:
        _display.step(message)
        if not status_callback:
            return
        if last_message["value"] == message:
            return
        last_message["value"] = message
        try:
            status_callback("thinking", message, True)
        except Exception:
            logger.exception("Failed to emit activity update")

    def _callback(step: ActionStep) -> None:
        in_tok = tracker.total_input_tokens
        out_tok = tracker.total_output_tokens
        step_num = getattr(step, "step_number", 0)
        timing = getattr(step, "timing", None)
        duration_s = None
        if timing is not None:
            start_time = getattr(timing, "start_time", None)
            end_time = getattr(timing, "end_time", None)
            if isinstance(start_time, (int, float)) and isinstance(
                end_time, (int, float)
            ):
                duration_s = max(0.0, end_time - start_time)

        logger.info(
            "[Step %d] Tokens so far: in=%s out=%s total=%s",
            step_num,
            f"{in_tok:,}",
            f"{out_tok:,}",
            f"{in_tok + out_tok:,}",
        )
        cost = getattr(tracker, "total_cost_usd", None)
        _display.token_summary(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_input_tokens=tracker.total_cached_input_tokens,
            step_number=step_num,
            duration_s=duration_s,
            cost_usd=cost,
        )

        if getattr(step, "is_final_answer", False):
            return
        if step.error:
            _emit("hit an error, retrying...")
            return
        tool_calls = getattr(step, "tool_calls", None) or []
        if tool_calls:
            tc = tool_calls[0]
            if isinstance(tc, dict):
                if "function" in tc:
                    tool_name = tc["function"].get("name", "unknown")
                else:
                    tool_name = tc.get("name", "unknown")
            elif hasattr(tc, "function") and tc.function is not None:
                tool_name = getattr(tc.function, "name", "unknown")
            else:
                tool_name = getattr(tc, "name", "unknown")
            _display.tool_call(tool_name)
            if status_callback:
                msg = tool_activity_message(tool_name)
                if last_message["value"] != msg:
                    last_message["value"] = msg
                    try:
                        status_callback("thinking", msg, True)
                    except Exception:
                        logger.exception("Failed to emit activity update")
            return
        _emit("thinking...")

    return _callback
