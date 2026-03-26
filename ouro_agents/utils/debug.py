"""Debug markdown serialization for agent run traces."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from smolagents import ActionStep
from smolagents.memory import TaskStep

from ..config import RunMode
from ..subagents.preflight import PreflightResult


def markdown_fence(content: str, lang: str = "text") -> str:
    """Fence ``content`` for embedding in markdown (handles nested ``` sequences)."""
    fence = "```"
    while fence in content:
        fence += "`"
    return f"{fence}{lang}\n{content}\n{fence}\n"


def serialize_memory_step_for_debug(step) -> str:
    """Format a single smolagents memory step into markdown for debug runs."""
    if isinstance(step, TaskStep):
        t = getattr(step, "task", "") or ""
        return f"## TaskStep\n\n{markdown_fence(t)}\n\n"
    if isinstance(step, ActionStep):
        parts: list[str] = []
        sn = getattr(step, "step_number", None)
        parts.append(
            f"## Action step {sn}\n\n" if sn is not None else "## Action step\n\n"
        )
        mo = getattr(step, "model_output", None) or ""
        if mo:
            parts.append("### Model output\n\n")
            parts.append(markdown_fence(mo))
            parts.append("\n\n")
        tcs = getattr(step, "tool_calls", None) or []
        if tcs:
            parts.append("### Tool calls\n\n")
            for tc in tcs:
                name = getattr(tc, "name", "?")
                args = getattr(tc, "arguments", None)
                try:
                    args_str = (
                        json.dumps(args, ensure_ascii=False) if args is not None else ""
                    )
                except TypeError:
                    args_str = str(args)
                if len(args_str) > 8000:
                    args_str = args_str[:8000] + "..."
                parts.append(f"- **{name}** `{args_str}`\n")
            parts.append("\n")
        obs = getattr(step, "observations", None) or ""
        if obs:
            obs_s = str(obs)
            cap = 80_000
            truncated = len(obs_s) > cap
            if truncated:
                obs_s = obs_s[:cap]
            parts.append("### Observations\n\n")
            parts.append(markdown_fence(obs_s))
            if truncated:
                parts.append("\n\n*(truncated)*\n")
            parts.append("\n\n")
        err = getattr(step, "error", None)
        if err:
            parts.append("### Error\n\n")
            parts.append(markdown_fence(str(err)))
            parts.append("\n\n")
        if getattr(step, "is_final_answer", False):
            parts.append("*Marked as final answer step.*\n\n")
        return "".join(parts)
    return f"## {type(step).__name__}\n\n{markdown_fence(repr(step))}\n\n"


def write_run_debug_markdown_preamble(
    path: Path,
    *,
    task: str,
    effective_task: str,
    full_system_prompt: str,
    run_id: str,
    mode: RunMode,
    preflight: Optional[PreflightResult],
) -> None:
    """Write header, system prompt, and task; run trace is appended after the agent finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Ouro Agents run debug\n\n",
        f"- **Timestamp (UTC):** {datetime.now(timezone.utc).isoformat()}\n",
        f"- **Run id:** `{run_id}`\n",
        f"- **Mode:** `{mode.value}`\n\n",
        "## Original task\n\n",
        markdown_fence(task),
        "\n## Effective task (what the agent sees)\n\n",
        markdown_fence(effective_task),
        "\n## Full system prompt\n\n",
        "This is the smolagents base template plus the Ouro-built soul prompt.\n\n",
        markdown_fence(full_system_prompt),
    ]
    if preflight is not None:
        lines.extend(
            [
                "\n## Preflight (step 0)\n\n",
                f"- **intent:** `{preflight.intent}`\n",
                f"- **complexity:** `{preflight.complexity}`\n",
                f"- **worth_remembering:** `{preflight.worth_remembering}`\n\n",
            ]
        )
        if preflight.briefing:
            lines.extend(
                ["### Briefing\n\n", markdown_fence(preflight.briefing), "\n"]
            )
        if preflight.plan:
            lines.extend(["### Plan\n\n", markdown_fence(preflight.plan), "\n"])
    else:
        lines.append("\n## Preflight (step 0)\n\n*(skipped or not applicable)*\n\n")

    lines.append("\n---\n\n## Agent steps (memory trace)\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(lines))


def append_run_debug_markdown_trace(path: Path, agent, result: str) -> None:
    """Append serialized memory steps and final result to a debug markdown file."""
    parts: list[str] = []
    for step in getattr(agent.memory, "steps", []) or []:
        parts.append(serialize_memory_step_for_debug(step))
    parts.append("## Final result\n\n")
    parts.append(markdown_fence(str(result)))
    parts.append("\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(parts))
