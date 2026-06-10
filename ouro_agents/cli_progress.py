"""Terminal progress display for long-running agent server tasks."""

from __future__ import annotations

import time
from typing import Any, Optional

from rich.markup import escape
from rich.status import Status

from .display import OuroDisplay, Verbosity, get_display
from .events import EventRunContext
from .observer import AgentObserver, ProgressEvent


class TerminalRunProgress(AgentObserver):
    """Render local CLI progress for a single webhook-backed agent run.

    The server still uses its normal observer for websocket publishing and
    persistence. This observer only owns local terminal presentation.
    """

    def __init__(
        self,
        event_run: EventRunContext,
        display: Optional[OuroDisplay] = None,
        *,
        enabled: Optional[bool] = None,
        config: Any = None,
    ) -> None:
        self.event_run = event_run
        self.display = display or get_display()
        self.config = config
        self.enabled = (
            self.display.verbosity >= Verbosity.NORMAL
            and bool(getattr(config, "enabled", True))
            if enabled is None
            else enabled
        )
        self.style = str(getattr(config, "style", "timeline"))
        self.show_spinner = bool(getattr(config, "show_spinner", True))
        self.show_prefetch = bool(getattr(config, "show_prefetch", True))
        self.show_token_updates = bool(getattr(config, "show_token_updates", True))
        self.show_subagents = bool(getattr(config, "show_subagents", True))
        self._started_at: float | None = None
        self._status: Status | None = None
        self._last_status_text: str | None = None
        self._phase_state: dict[str, str] = {}

    def start(self) -> None:
        if not self.enabled:
            return
        self._started_at = time.monotonic()
        self.display.blank()
        self.display.rule(f"{self.event_run.event_type} event")
        for line in self._event_summary_lines():
            self.display.info(line)
        self.on_progress(
            ProgressEvent(
                phase="received_event",
                message="accepted webhook event",
                state="complete",
            )
        )
        self._update_status("event", "preparing run")

    def finish(self, result_text: str | None = None) -> None:
        if not self.enabled:
            return
        self._stop_status()
        outcome = "no action" if (result_text or "").strip() == "NO_ACTION" else "done"
        self.on_progress(
            ProgressEvent("complete", f"run {outcome}", "complete")
        )

    def cancel(self, reason: str = "cancelled") -> None:
        if not self.enabled:
            return
        self._stop_status()
        self.display.info(f"run {reason} ({self._elapsed_label()})")

    def fail(self, error: str = "failed") -> None:
        if not self.enabled:
            return
        self._stop_status()
        self.display.error(f"run {error} ({self._elapsed_label()})")

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        if not self.enabled:
            return
        if self.style == "debug":
            state = "active" if active else "complete"
            self._render_line(state, "activity", message or status)
        if active:
            self._update_status(status, message)
        else:
            self._stop_status()

    def on_result_ready(self, result_text: str) -> None:
        if not self.enabled:
            return
        if result_text == "NO_ACTION":
            self._update_status("result", "decided no action")
        else:
            self._update_status("result", "finalizing response")

    def on_progress(self, event: ProgressEvent) -> None:
        if not self.enabled:
            return
        if event.phase == "prefetching_context" and not self.show_prefetch:
            return
        if event.phase == "token_update" and not self.show_token_updates:
            return
        if event.phase.startswith("subagent_") and not self.show_subagents:
            return

        label = self._event_label(event)
        if event.state == "active":
            self._update_status(event.phase, label)

        if self.style == "compact" and event.phase != "complete":
            return
        if self._should_render_event(event):
            self._render_line(event.state, self._phase_title(event.phase), label)
        self._phase_state[event.phase] = event.state

    def _event_summary_lines(self) -> list[str]:
        lines: list[str] = []
        actor = f"@{self.event_run.actor_username}" if self.event_run.actor_username else ""
        subject = self._subject_label()
        parts = [part for part in (actor, subject) if part]
        if parts:
            lines.append("trigger: " + " on ".join(parts))

        scope_parts = []
        if self.event_run.team_id:
            scope_parts.append(f"team={self._short_id(self.event_run.team_id)}")
        if self.event_run.conversation_id:
            scope_parts.append(
                f"conversation={self._short_id(self.event_run.conversation_id)}"
            )
        if scope_parts:
            lines.append("scope: " + " | ".join(scope_parts))

        tool_names = self._preload_tool_names()
        if tool_names:
            suffix = "" if len(tool_names) <= 6 else f" +{len(tool_names) - 6} more"
            lines.append("ready tools: " + ", ".join(tool_names[:6]) + suffix)

        prefetch = self._prefetch_summary()
        if prefetch and self.show_prefetch:
            lines.append("prefetch: " + prefetch)
        return lines or ["trigger: webhook event"]

    def _subject_label(self) -> str:
        asset_type = self.event_run.root_asset_type or self.event_run.asset_type
        asset_id = self.event_run.root_asset_id or self.event_run.asset_id
        if asset_type and asset_id:
            return f"{asset_type}:{self._short_id(asset_id)}"
        if asset_id:
            return self._short_id(asset_id)
        return ""

    def _preload_tool_names(self) -> list[str]:
        names: list[str] = []
        for name in self.event_run.preload_tools or ():
            leaf = str(name).split(":", 1)[-1]
            if leaf and leaf not in names:
                names.append(leaf)
        return names

    def _prefetch_summary(self) -> str:
        spec = self.event_run.prefetch
        if spec.empty:
            return ""
        parts: list[str] = []
        if spec.asset_ids:
            parts.append(self._plural(len(spec.asset_ids), "asset"))
        if spec.comment_parent_ids:
            parts.append(self._plural(len(spec.comment_parent_ids), "comment thread"))
        if spec.thread_comment_parent_ids:
            parts.append(
                self._plural(len(spec.thread_comment_parent_ids), "reply thread")
            )
        if spec.focus_comment_id:
            parts.append(f"focus={self._short_id(spec.focus_comment_id)}")
        return ", ".join(parts)

    def _update_status(self, status: str, message: Optional[str]) -> None:
        if not self.show_spinner:
            return
        label = self._format_status(status, message)
        if label == self._last_status_text:
            return
        self._last_status_text = label
        if not self.display.console.is_terminal:
            return
        if self._status is None:
            self._status = self.display.console.status(
                label,
                spinner="dots",
                spinner_style="ouro.accent",
            )
            self._status.start()
            return
        self._status.update(label)

    def _stop_status(self) -> None:
        if self._status is None:
            return
        self._status.stop()
        self._status = None

    def _should_render_event(self, event: ProgressEvent) -> bool:
        if self.style == "debug":
            return True
        if event.phase == "token_update":
            return self.show_token_updates
        prior = self._phase_state.get(event.phase)
        if event.state == "active":
            return prior is None
        return prior != event.state

    def _render_line(self, state: str, title: str, message: str) -> None:
        elapsed = self._elapsed_label()
        suffix = f" ({elapsed})" if state in {"complete", "failed"} else ""
        line = f"{title}: {message}{suffix}" if message else f"{title}{suffix}"
        if state == "failed":
            self.display.error(f"x {line}")
        elif state == "complete":
            self.display.success(f"ok {line}")
        else:
            self.display.info(f"> {line}")

    def _event_label(self, event: ProgressEvent) -> str:
        message = event.message or self._phase_title(event.phase)
        if event.phase.startswith("subagent_"):
            message = self._subagent_label(event, message)
        if event.phase == "token_update":
            message = self._token_label(event)
        return message

    def _subagent_label(self, event: ProgressEvent, fallback: str) -> str:
        name = event.detail.get("name") or event.detail.get("subagent")
        if not name:
            return fallback
        max_steps = event.detail.get("max_steps")
        if event.phase == "subagent_started":
            suffix = f" (max_steps={max_steps})" if max_steps else ""
            return f"{name}{suffix}"
        if event.phase == "subagent_completed":
            parts = [str(name)]
            usage = event.detail.get("usage") or {}
            if usage.get("steps"):
                parts.append(f"{usage['steps']} steps")
            if usage.get("total_tokens"):
                parts.append(f"{usage['total_tokens']:,} tok")
            if usage.get("current_context_tokens"):
                parts.append(f"ctx {usage['current_context_tokens']:,}")
            if usage.get("cost_usd") is not None:
                parts.append(f"${usage['cost_usd']:.6f}")
            asset = event.detail.get("asset")
            if asset:
                parts.append(str(asset))
            return " | ".join(parts)
        if event.phase == "subagent_failed":
            error = event.detail.get("error") or fallback
            return f"{name}: {error}"
        return fallback

    def _token_label(self, event: ProgressEvent) -> str:
        detail = event.detail
        total = detail.get("total_tokens")
        if total is None:
            return event.message or "token update"
        parts = [f"{total:,} tok"]
        if detail.get("current_context_tokens"):
            parts.append(f"ctx {detail['current_context_tokens']:,}")
        if detail.get("input_tokens"):
            parts.append(f"in {detail['input_tokens']:,}")
        if detail.get("output_tokens"):
            parts.append(f"out {detail['output_tokens']:,}")
        if detail.get("cost_usd") is not None:
            parts.append(f"${detail['cost_usd']:.6f}")
        return " | ".join(parts)

    @staticmethod
    def _phase_title(phase: str) -> str:
        titles = {
            "received_event": "received event",
            "marking_notifications": "notifications",
            "prefetching_context": "context",
            "preflight": "preflight",
            "building_tools": "tools",
            "building_prompt": "prompt",
            "running_agent": "agent",
            "persisting_response": "response",
            "reflecting": "reflection",
            "complete": "complete",
            "token_update": "usage",
            "subagent_started": "subagent",
            "subagent_step": "subagent",
            "subagent_completed": "subagent",
            "subagent_failed": "subagent",
        }
        return titles.get(phase, phase.replace("_", " "))

    @staticmethod
    def _format_status(status: str, message: Optional[str]) -> str:
        detail = (message or status or "working").strip()
        return f"[ouro.accent]{escape(status)}[/] [ouro.muted]{escape(detail)}[/]"

    @staticmethod
    def _plural(count: int, label: str) -> str:
        suffix = "" if count == 1 else "s"
        return f"{count} {label}{suffix}"

    @staticmethod
    def _short_id(value: str) -> str:
        if len(value) <= 14:
            return value
        return f"{value[:8]}...{value[-4:]}"

    def _elapsed_label(self) -> str:
        if self._started_at is None:
            return "0.0s"
        return f"{max(0.0, time.monotonic() - self._started_at):.1f}s"
