"""Clean, modern CLI display for ouro-agents.

Replaces smolagents' default Rich-heavy output with a minimal aesthetic
inspired by Claude Code: muted colors, thin rules, compact step indicators.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Literal, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from smolagents.monitoring import AgentLogger, LogLevel

from .subagents.context import SubAgentUsage
from .usage import RunUsage, residual_main_usage

THEME = Theme(
    {
        "ouro.dim": "dim white",
        "ouro.accent": "#6B8AFF",
        "ouro.tool": "#6B8AFF bold",
        "ouro.step": "dim cyan",
        "ouro.error": "red",
        "ouro.success": "green",
        "ouro.muted": "#888888",
        "ouro.bold": "bold white",
        "ouro.prompt": "bold #6B8AFF",
        "ouro.rule": "#444444",
    }
)

RULE_CHAR = "─"


class Verbosity(IntEnum):
    QUIET = 0
    NORMAL = 1
    VERBOSE = 2


def _to_log_level(verbosity: Verbosity) -> LogLevel:
    return {
        Verbosity.QUIET: LogLevel.ERROR,
        Verbosity.NORMAL: LogLevel.INFO,
        Verbosity.VERBOSE: LogLevel.DEBUG,
    }[verbosity]


class OuroDisplay:
    """Owns all CLI terminal output formatting."""

    def __init__(self, verbosity: Verbosity = Verbosity.NORMAL):
        self.verbosity = verbosity
        self.console = Console(
            theme=THEME,
            highlight=False,
            stderr=False,
        )

    def rule(self, title: str = "") -> None:
        if title:
            self.console.rule(
                f"[ouro.muted]{title}[/]",
                characters=RULE_CHAR,
                style="ouro.rule",
            )
        else:
            self.console.rule(characters=RULE_CHAR, style="ouro.rule")

    def blank(self) -> None:
        self.console.print()

    def header(self, title: str, subtitle: str = "") -> None:
        self.blank()
        self.console.print(f"[ouro.bold]{title}[/]")
        if subtitle:
            self.console.print(f"[ouro.muted]{subtitle}[/]")
        self.rule()
        self.blank()

    def info(self, message: str) -> None:
        self.console.print(f"[ouro.muted]{escape(message)}[/]")

    def success(self, message: str) -> None:
        self.console.print(f"[ouro.success]{escape(message)}[/]")

    def error(self, message: str) -> None:
        self.console.print(f"[ouro.error]{escape(message)}[/]")

    def step(self, message: str) -> None:
        if self.verbosity >= Verbosity.NORMAL:
            self.console.print(f"  [ouro.step]> {escape(message)}[/]")

    def tool_call(self, tool_name: str) -> None:
        if self.verbosity >= Verbosity.NORMAL:
            self.console.print(f"  [ouro.step]>[/] [ouro.tool]{escape(tool_name)}[/]")

    def observation(self, text: str) -> None:
        """Render tool observation/result as markdown."""
        if self.verbosity < Verbosity.NORMAL:
            return
        self.console.print(f"  [ouro.dim]observation:[/]")
        md = Markdown(text, code_theme="monokai")
        self.console.print(md)

    def _log_tool_call(self, raw: str) -> None:
        """Parse smolagents' 'Calling tool:' text into our compact format."""
        if self.verbosity < Verbosity.NORMAL:
            return
        m = re.search(r"Calling tool:\s*'([^']+)'", raw)
        name = m.group(1) if m else "unknown"
        args_match = re.search(r"with arguments:\s*(.+)", raw, re.DOTALL)
        args_str = args_match.group(1).strip() if args_match else ""
        if args_str:
            self.console.print(
                f"  [ouro.step]>[/] [ouro.tool]{escape(name)}[/]"
                f"[ouro.dim]({escape(args_str)})[/]"
            )
        else:
            self.tool_call(name)

    def token_summary(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        step_number: int = 0,
        duration_s: float | None = None,
        cost_usd: Optional[float] = None,
    ) -> None:
        if self.verbosity < Verbosity.NORMAL:
            return
        total = input_tokens + output_tokens
        parts = [f"step {step_number}"] if step_number else []
        if duration_s is not None:
            parts.append(f"{duration_s:.2f}s")
        parts.append(f"{total:,} tok")
        if input_tokens:
            parts.append(f"in {input_tokens:,}")
        if cached_input_tokens:
            parts.append(f"cached {cached_input_tokens:,}")
            parts.append(f"uncached {max(0, input_tokens - cached_input_tokens):,}")
        if output_tokens:
            parts.append(f"out {output_tokens:,}")
        if cost_usd is not None:
            parts.append(f"${cost_usd:.6f}")
        self.console.print(f"  [ouro.dim]({' | '.join(parts)})[/]")

    @staticmethod
    def _run_usage_detail_parts(usage: RunUsage) -> list[str]:
        parts: list[str] = []
        if usage.total_tokens:
            parts.append(f"{usage.total_tokens:,} tok")
        if usage.input_tokens:
            parts.append(f"in {usage.input_tokens:,}")
        if usage.cached_input_tokens:
            parts.append(f"cached {usage.cached_input_tokens:,}")
            parts.append(f"uncached {usage.uncached_input_tokens:,}")
        if usage.output_tokens:
            parts.append(f"out {usage.output_tokens:,}")
        if usage.reasoning_tokens:
            parts.append(f"reasoning {usage.reasoning_tokens:,}")
        if usage.num_api_calls:
            parts.append(f"calls {usage.num_api_calls}")
        if usage.cost_usd is not None:
            parts.append(f"${usage.cost_usd:.6f}")
        if usage.input_cost_usd is not None:
            parts.append(f"in ${usage.input_cost_usd:.6f}")
        if usage.output_cost_usd is not None:
            parts.append(f"out ${usage.output_cost_usd:.6f}")
        return parts

    @staticmethod
    def _subagent_usage_detail_parts(u: SubAgentUsage) -> list[str]:
        parts: list[str] = []
        total_tok = u.total_tokens
        if total_tok:
            parts.append(f"{total_tok:,} tok")
        if u.input_tokens:
            parts.append(f"in {u.input_tokens:,}")
        if u.cached_input_tokens:
            parts.append(f"cached {u.cached_input_tokens:,}")
            parts.append(f"uncached {u.uncached_input_tokens:,}")
        if u.output_tokens:
            parts.append(f"out {u.output_tokens:,}")
        if u.reasoning_tokens:
            parts.append(f"reasoning {u.reasoning_tokens:,}")
        if u.llm_calls:
            parts.append(f"calls {u.llm_calls}")
        if u.cost_usd is not None:
            parts.append(f"${u.cost_usd:.6f}")
        if u.input_cost_usd is not None:
            parts.append(f"in ${u.input_cost_usd:.6f}")
        if u.output_cost_usd is not None:
            parts.append(f"out ${u.output_cost_usd:.6f}")
        return parts

    def run_summary(
        self,
        usage: RunUsage,
        duration_s: float | None = None,
        subagent_ledger: Optional[list[tuple[str, SubAgentUsage]]] = None,
    ) -> None:
        def _emit_dim_line(label: str, detail_parts: list[str], dur: float | None):
            seg = " · ".join(detail_parts)
            if dur is not None:
                seg = f"{seg} · {dur:.1f}s" if seg else f"{dur:.1f}s"
            self.console.print(f"  [ouro.dim]{label}[/] {seg}")

        if not subagent_ledger:
            parts = self._run_usage_detail_parts(usage)
            if duration_s is not None:
                parts.append(f"{duration_s:.1f}s")
            if parts:
                self.blank()
                self.console.print(f"[ouro.dim]{' · '.join(parts)}[/]")
            return

        self.blank()
        main_parts = self._run_usage_detail_parts(
            residual_main_usage(usage, subagent_ledger)
        )
        _emit_dim_line("main", main_parts, None)
        for name, su in subagent_ledger:
            _emit_dim_line(f"sub:{name}", self._subagent_usage_detail_parts(su), None)
        total_parts = self._run_usage_detail_parts(usage)
        _emit_dim_line("task total", total_parts, duration_s)

    def markdown(self, text: str) -> None:
        """Render markdown content with proper formatting (headers, bold, lists, code, etc.)."""
        md = Markdown(text, code_theme="monokai")
        self.console.print(md)

    def response(self, text: str) -> None:
        self.blank()
        self.markdown(text)

    def code_block(self, code: str, language: str = "python") -> None:
        syntax = Syntax(
            code,
            lexer=language,
            theme="monokai",
            word_wrap=True,
            padding=(0, 2),
        )
        self.console.print(syntax)

    def prompt(self, prompt_text: str = "you") -> str:
        try:
            return self.console.input(f"[ouro.prompt]{prompt_text}>[/] ").strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    def chat_header(self, conversation_id: str) -> None:
        self.header("ouro-agents", f"conversation {conversation_id}")
        self.console.print(
            "[ouro.dim]commands: /exit /quit /new /conversation <id>[/]"
        )
        self.blank()

    def chat_response(self, text: str) -> None:
        self.blank()
        self.console.print(f"[ouro.bold]agent[/]")
        self.markdown(text)
        self.blank()

    def run_header(self, task: str) -> None:
        self.header("ouro-agents run")
        self.console.print(f"[ouro.dim]task:[/] {escape(task)}")
        self.blank()

    def run_result(self, result: str) -> None:
        self.blank()
        self.rule("result")
        self.blank()
        self.markdown(result)
        self.blank()

    def heartbeat_result(self, result: str | None) -> None:
        self.blank()
        if result:
            self.rule("heartbeat")
            self.blank()
            self.markdown(result)
        else:
            self.info("heartbeat: no action taken")
        self.blank()

    def planning_result(self, result: str | None) -> None:
        self.blank()
        if result:
            self.rule("planning")
            self.blank()
            self.markdown(result)
        else:
            self.info("planning: no plan generated")
        self.blank()

    def review_result(self, result: str | None) -> None:
        self.blank()
        if result:
            self.rule("review")
            self.blank()
            self.markdown(result)
        else:
            self.info("review: no plan to review")
        self.blank()


_display: OuroDisplay | None = None


def get_display(verbosity: Verbosity = Verbosity.NORMAL) -> OuroDisplay:
    """Get or create the global display singleton."""
    global _display
    if _display is None:
        _display = OuroDisplay(verbosity)
    return _display


def set_display(display: OuroDisplay) -> None:
    global _display
    _display = display


class OuroLogger(AgentLogger):
    """Custom AgentLogger that produces clean, minimal output.

    Overrides all the Rich-heavy methods from smolagents' AgentLogger
    with compact, muted formatting.
    """

    def __init__(
        self,
        level: LogLevel = LogLevel.INFO,
        display: OuroDisplay | None = None,
        *,
        show_final_answer: bool = False,
    ):
        self._display = display or get_display()
        self.console = self._display.console
        self.level = level
        self.show_final_answer = show_final_answer

    def log(self, *args, level: int | str | LogLevel = LogLevel.INFO, **kwargs) -> None:
        if isinstance(level, str):
            level = LogLevel[level.upper()]
        if level > self.level:
            return

        for arg in args:
            if isinstance(arg, Text):
                plain = arg.plain
                if plain.startswith("Final answer:"):
                    if self.show_final_answer:
                        body = plain[len("Final answer:") :].strip()
                        if body:
                            self._display.observation(body)
                    return
                if plain.startswith("[Step "):
                    return

            if isinstance(arg, Panel):
                renderable = arg.renderable
                plain = renderable.plain if isinstance(renderable, Text) else str(renderable)
                if "Calling tool:" in plain and "'final_answer'" in plain and not self.show_final_answer:
                    return
                if "Calling tool:" in plain:
                    self._display._log_tool_call(plain)
                    return

            if isinstance(arg, str):
                if arg.startswith("Observations:"):
                    body = arg[len("Observations:"):].strip()
                    if body:
                        self._display.observation(body)
                    return

        self.console.print(*args, **kwargs)

    def log_error(self, error_message: str) -> None:
        self._display.error(error_message)

    def log_markdown(
        self,
        content: str,
        title: str | None = None,
        level=LogLevel.INFO,
        style=None,
    ) -> None:
        if level > self.level:
            return
        if title:
            self._display.step(title)
        self._display.markdown(content)

    def log_code(self, title: str, content: str, level: int = LogLevel.INFO) -> None:
        if level > self.level:
            return
        self._display.step(title)
        self._display.code_block(content)

    def log_rule(self, title: str, level: int = LogLevel.INFO) -> None:
        if level > self.level:
            return
        self._display.rule(title)

    def log_task(
        self,
        content: str,
        subtitle: str,
        title: str | None = None,
        level: LogLevel = LogLevel.INFO,
    ) -> None:
        if level > self.level:
            return
        label = title or "New run"
        self._display.blank()
        self._display.rule(label)
        if self._display.verbosity >= Verbosity.VERBOSE:
            self._display.info(content[:200])
        self._display.blank()

    def log_messages(self, messages: list[dict], level: LogLevel = LogLevel.DEBUG) -> None:
        if level > self.level:
            return
        for msg in messages[:3]:
            d = msg.dict() if hasattr(msg, "dict") else msg
            role = d.get("role", "?")
            content = str(d.get("content", ""))[:120]
            self._display.info(f"  {role}: {content}")

    def visualize_agent_tree(self, agent) -> None:
        tools = list(getattr(agent, "tools", {}).keys())
        model_id = getattr(getattr(agent, "model", None), "model_id", "?")
        self._display.info(f"agent: {agent.__class__.__name__} | {model_id}")
        if tools:
            self._display.info(f"tools: {', '.join(tools)}")


def create_logger(
    verbosity: Verbosity = Verbosity.NORMAL,
    display: OuroDisplay | None = None,
    *,
    show_final_answer: bool = False,
) -> OuroLogger:
    """Create an OuroLogger at the given verbosity."""
    return OuroLogger(
        level=_to_log_level(verbosity),
        display=display,
        show_final_answer=show_final_answer,
    )


def create_quiet_logger(display: OuroDisplay | None = None) -> OuroLogger:
    """Create a logger that only shows errors (for subagents)."""
    return OuroLogger(level=LogLevel.OFF, display=display)


SubagentLogLevelName = Literal["off", "error", "info", "debug"]


def create_subagent_logger(
    level_name: SubagentLogLevelName | str,
    display: OuroDisplay | None = None,
    *,
    show_final_answer: bool | None = None,
) -> OuroLogger:
    """OuroLogger for a subagent ``ToolCallingAgent``, at a fixed smolagents level.

    ``level_name`` maps to ``LogLevel.OFF|ERROR|INFO|DEBUG``. When ``off``,
    equivalent to :func:`create_quiet_logger`.

    If ``show_final_answer`` is omitted, it defaults to ``True`` whenever the
    level is not ``off`` (so ``final_answer`` tool output is visible for traced runs).
    """
    key = str(level_name).strip().lower()
    mapping: dict[str, LogLevel] = {
        "off": LogLevel.OFF,
        "error": LogLevel.ERROR,
        "info": LogLevel.INFO,
        "debug": LogLevel.DEBUG,
    }
    if key not in mapping:
        valid = ", ".join(sorted(mapping))
        raise ValueError(
            f"Invalid subagent log level {level_name!r}; expected one of: {valid}"
        )
    level = mapping[key]
    _display = display or get_display()
    if level == LogLevel.OFF:
        return create_quiet_logger(_display)
    sf = True if show_final_answer is None else show_final_answer
    return OuroLogger(level=level, display=_display, show_final_answer=sf)
