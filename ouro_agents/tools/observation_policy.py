"""Spill oversized tool observations to disk; keep tight stubs in agent memory.

History stays append-only for prompt-cache friendliness: new tool results are
either kept as-is or replaced with a head/tail stub that points at a workspace
file. Older steps are only rewritten on a rare one-shot compact when a hard
ceiling is crossed.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

SPILL_DIR = Path("scratch") / "tool-outputs"
SPILL_MARKER_PREFIX = "[tool output spilled:"
# Captures the relative spill path from a stub header line.
SPILL_PATH_RE = re.compile(
    r"\[tool output spilled:\s*[\d,]+\s*chars\s*→\s*(.+?)\]"
)
RUN_COMPACT_MARKER = "[older observation folded to save context"

# Tools whose results are intentional context (skill bodies, etc.) and should
# never be replaced with a head/tail stub.
DEFAULT_EXEMPT_TOOLS: tuple[str, ...] = ("load_skill",)


@dataclass(frozen=True)
class ObservationPolicy:
    """In-memory policy knobs (mirrors ObservationPolicyConfig)."""

    max_inline_chars: int = 40_000
    head_chars: int = 3_000
    tail_chars: int = 1_500
    max_step_chars: int = 40_000
    run_compact_ceiling: int = 200_000
    keep_recent_steps: int = 3
    excerpt_chars: int = 3_000
    exempt_tools: tuple[str, ...] = DEFAULT_EXEMPT_TOOLS


def _normalize_exempt_tools(tools: Sequence[str] | None) -> tuple[str, ...]:
    if not tools:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for name in tools:
        key = str(name or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)


def is_exempt_tool(tool_name: str, policy: ObservationPolicy) -> bool:
    """Return True when this tool's results should stay fully inline."""
    name = str(tool_name or "").strip()
    if not name:
        return False
    exempt = set(policy.exempt_tools)
    if name in exempt:
        return True
    # Bare name match for qualified MCP-style names if ever listed that way.
    if ":" in name and name.split(":", 1)[-1] in exempt:
        return True
    return False


_spill_seq = 0
_spill_lock = threading.Lock()


def _next_spill_seq() -> int:
    global _spill_seq
    with _spill_lock:
        _spill_seq += 1
        return _spill_seq


def _safe_tool_slug(tool_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name or "tool").strip("._")
    return (slug or "tool")[:64]


def _pick_extension(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return ".json"
    return ".txt"


def format_observation_budget_hint(policy: ObservationPolicy) -> str:
    """Short system-prompt note so the agent can size shell/file reads."""
    lines = [
        "Tool-result size budget:",
        f"- Results over {policy.max_inline_chars:,} characters are spilled to "
        f"`scratch/tool-outputs/` and replaced with a head/tail stub "
        f"({policy.head_chars:,} + {policy.tail_chars:,} chars kept inline).",
        f"- When you need more from a spill file (or any large file), keep the "
        f"follow-up tool output under {policy.max_inline_chars:,} characters — "
        "use `head`/`tail`, a bounded `sed -n` range, or `rg` with a line/byte "
        "window. Do not `cat` the whole spill file.",
    ]
    if policy.exempt_tools:
        names = ", ".join(f"`{n}`" for n in policy.exempt_tools)
        lines.append(
            f"- Exempt from spilling (always fully inline): {names}."
        )
    return "\n".join(lines)


def build_spill_stub(
    *,
    relative_path: str,
    text: str,
    head_chars: int,
    tail_chars: int,
    max_inline_chars: int,
) -> str:
    """Build the inline observation stub that points at a spilled file."""
    total = len(text)
    head_n = max(0, head_chars)
    tail_n = max(0, tail_chars)
    if total <= head_n + tail_n:
        body = text
        showing = f"Showing all {total:,} chars"
    else:
        head = text[:head_n]
        tail = text[-tail_n:] if tail_n else ""
        body = f"{head}\n\n... [{total - head_n - tail_n:,} chars omitted] ...\n\n{tail}"
        showing = f"Showing first {head_n:,} and last {tail_n:,} chars"

    return (
        f"{SPILL_MARKER_PREFIX} {total:,} chars → {relative_path}]\n"
        f"{showing}. Inline limit is {max_inline_chars:,} chars — keep follow-up "
        "reads under that (head/tail, bounded sed/rg); do not cat the whole file.\n\n"
        f"--- head ---\n{body}"
    )


def spill_and_stub(
    text: str,
    *,
    tool_name: str,
    workspace: Path,
    run_id: str,
    policy: ObservationPolicy,
    seq: Optional[int] = None,
) -> str:
    """Write ``text`` under scratch/tool-outputs and return a head/tail stub."""
    workspace = Path(workspace).resolve()
    run_key = (run_id or "run").replace("/", "_")[:64]
    seq_num = seq if seq is not None else _next_spill_seq()
    ext = _pick_extension(text)
    filename = f"{seq_num:04d}-{_safe_tool_slug(tool_name)}{ext}"
    rel = SPILL_DIR / run_key / filename
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    stub = build_spill_stub(
        relative_path=rel.as_posix(),
        text=text,
        head_chars=policy.head_chars,
        tail_chars=policy.tail_chars,
        max_inline_chars=policy.max_inline_chars,
    )
    logger.info(
        "Spilled tool '%s' output: %d chars → %s (stub %d chars)",
        tool_name,
        len(text),
        rel.as_posix(),
        len(stub),
    )
    return stub


def maybe_spill_and_stub(
    text: str,
    *,
    tool_name: str,
    workspace: Optional[Path],
    run_id: str,
    policy: ObservationPolicy,
) -> str:
    """Spill when over ``max_inline_chars``; otherwise return ``text`` unchanged.

    Exempt tools (see ``policy.exempt_tools``) always stay fully inline — their
    payloads are intentional context (e.g. skill bodies from ``load_skill``).

    If ``workspace`` is missing, fall back to a head/tail stub without a file
    (still bounds what enters memory).
    """
    if is_exempt_tool(tool_name, policy):
        return text
    if len(text) <= policy.max_inline_chars:
        return text
    if workspace is None:
        logger.warning(
            "Tool '%s' returned %d chars but no workspace for spill; "
            "emitting head/tail stub only",
            tool_name,
            len(text),
        )
        return build_spill_stub(
            relative_path="(no workspace — output not saved)",
            text=text,
            head_chars=policy.head_chars,
            tail_chars=policy.tail_chars,
            max_inline_chars=policy.max_inline_chars,
        )
    return spill_and_stub(
        text,
        tool_name=tool_name,
        workspace=workspace,
        run_id=run_id,
        policy=policy,
    )


def extract_spill_paths(observations: str) -> list[str]:
    """Return relative spill paths mentioned in an observation blob."""
    return SPILL_PATH_RE.findall(observations or "")


def fold_observation_excerpt(
    observations: str,
    *,
    excerpt_chars: int,
    max_inline_chars: int,
) -> str:
    """Reduce an old step's observations for one-shot compact; keep spill paths."""
    text = observations or ""
    if RUN_COMPACT_MARKER in text:
        return text
    paths = extract_spill_paths(text)
    excerpt = text[: max(0, excerpt_chars)]
    path_lines = ""
    if paths:
        unique = list(dict.fromkeys(paths))
        path_lines = "\nSpill files: " + ", ".join(unique)
    return (
        f"{excerpt}\n\n{RUN_COMPACT_MARKER}: "
        f"{len(text):,} chars originally; re-read spill files under the "
        f"{max_inline_chars:,}-char inline limit (head/tail/bounded sed/rg), "
        f"or re-run the tool if full output is needed again]{path_lines}"
    )


def enforce_step_budget(
    observations: str,
    *,
    tool_name: str,
    workspace: Optional[Path],
    run_id: str,
    policy: ObservationPolicy,
) -> str:
    """If a combined step observation exceeds ``max_step_chars``, spill it.

    When the blob is labeled with per-call headers, spill oversized non-exempt
    sections individually so attribution (and exempt skill bodies) survive.
    """
    if not observations or len(observations) <= policy.max_step_chars:
        return observations

    from ..utils.tool_observations import (
        format_tool_result_header,
        split_labeled_observation_sections,
    )

    sections = split_labeled_observation_sections(observations)
    if not sections:
        return maybe_spill_and_stub(
            observations,
            tool_name=tool_name or "step",
            workspace=workspace,
            run_id=run_id,
            policy=policy,
        )

    rebuilt: list[str] = []
    for name, call_id, body in sections:
        header = format_tool_result_header(name, call_id or "unknown")
        if is_exempt_tool(name, policy) or len(body) <= policy.max_inline_chars:
            rebuilt.append(f"{header}\n{body}" if body else header)
            continue
        stub = maybe_spill_and_stub(
            body,
            tool_name=name or tool_name or "step",
            workspace=workspace,
            run_id=run_id,
            policy=policy,
        )
        rebuilt.append(f"{header}\n{stub}" if stub else header)
    return "\n".join(rebuilt)


def to_observation_policy(config) -> ObservationPolicy:
    """Build an ``ObservationPolicy`` from config (or defaults)."""
    if config is None:
        return ObservationPolicy()
    exempt = getattr(config, "exempt_tools", None)
    if exempt is None:
        exempt_tools = DEFAULT_EXEMPT_TOOLS
    else:
        exempt_tools = _normalize_exempt_tools(exempt)
    return ObservationPolicy(
        max_inline_chars=int(config.max_inline_chars),
        head_chars=int(config.head_chars),
        tail_chars=int(config.tail_chars),
        max_step_chars=int(config.max_step_chars),
        run_compact_ceiling=int(config.run_compact_ceiling),
        keep_recent_steps=int(config.keep_recent_steps),
        excerpt_chars=int(config.excerpt_chars),
        exempt_tools=exempt_tools,
    )
