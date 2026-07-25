"""Refinement runner — drains the change-set queue with a cheap LLM.

Doc-scoping discipline:
  1. Discover candidate docs by grepping for each pending entry's subject_id.
  2. For each candidate, build a *scoped view*: the frontmatter (verbatim),
     a heading TOC, and ±N-line "windows" around each subject match. The full
     doc body is never sent to the LLM.
  3. The model returns anchored window replacements + an optional list of
     mem0 memory_ids to delete. Anchors map back to the original line ranges.
  4. We apply replacements back into the file in reverse line order so
     earlier edits don't shift later anchors. Frontmatter ``last_updated``
     is bumped so the workspace sync pushes the change to Ouro.

The runner is self-contained — it does not invoke the smolagents agent loop.
That keeps the cost predictable and means there is no LLM tool surface to
defend during a refinement pass.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from ..memory.frontmatter import (
    parse_frontmatter_timestamp,
    set_frontmatter_timestamp,
    strip_frontmatter,
)
from ..constants import parse_llm_json
from .queue import ChangeEntry, ChangeKind, ChangeSetQueue

if TYPE_CHECKING:  # pragma: no cover
    from ..agent import OuroAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class WindowResult:
    path: str
    anchor: int
    applied: bool
    note: str = ""


@dataclass
class RefinementSummary:
    pending_seen: int = 0
    docs_inspected: int = 0
    files_rewritten: list[str] = field(default_factory=list)
    windows_applied: int = 0
    memory_deletes: int = 0
    queue_marked_applied: int = 0
    per_doc_summaries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Truncated prompt/response traces for dream audits (and CLI debugging).
    llm_calls: list[dict] = field(default_factory=list)

    @property
    def did_anything(self) -> bool:
        return bool(self.files_rewritten or self.memory_deletes)


# ---------------------------------------------------------------------------
# Scoped views
# ---------------------------------------------------------------------------


_SWEEP_EXCLUDE_DIRS = {
    "protected",
    "chroma",
    "memory",
    "memory-old",
    "__pycache__",
    "cifs",
    "cifs_old",
    "debug-runs",
    "conversations",
    "daily_logs_archive",
}


@dataclass
class Window:
    anchor: int
    start_line: int  # 1-indexed inclusive
    end_line: int  # 1-indexed inclusive
    text: str
    matched_subjects: list[str]


@dataclass
class DocView:
    path: Path
    frontmatter: str
    body_lines: list[str]
    toc: list[str]
    windows: list[Window]
    related_changes: list[ChangeEntry]


def _ripgrep_files_with(subjects: Iterable[str], root: Path) -> Optional[set[Path]]:
    """Use ripgrep to enumerate files containing any of ``subjects`` under ``root``."""
    rg = shutil.which("rg")
    subject_list = [s for s in subjects if s]
    if not rg or not subject_list:
        return None
    cmd = [
        rg,
        "--files-with-matches",
        "--fixed-strings",
        "--no-messages",
        "--hidden",
        # Agent workspaces are commonly gitignored by the parent repo, which
        # would make ripgrep skip them silently. _SWEEP_EXCLUDE_DIRS handles
        # noise control on its own.
        "--no-ignore",
    ]
    for excluded in _SWEEP_EXCLUDE_DIRS:
        cmd.extend(["--glob", f"!**/{excluded}/**"])
    for pattern in subject_list:
        cmd.extend(["-e", pattern])
    cmd.append(str(root))
    try:
        completed = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=30
        )
    except subprocess.SubprocessError as exc:
        logger.warning("ripgrep failed for refinement scan: %s", exc)
        return None
    if completed.returncode not in (0, 1):
        return None
    return {Path(line) for line in completed.stdout.splitlines() if line.strip()}


def _python_walk_files_with(subjects: Iterable[str], root: Path) -> set[Path]:
    subject_list = [s for s in subjects if s]
    if not subject_list:
        return set()
    matches: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SWEEP_EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix not in {".md"}:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if any(s in text for s in subject_list):
            matches.add(path)
    return matches


def collect_affected_docs(
    workspace: Path, pending: list[ChangeEntry]
) -> dict[Path, list[ChangeEntry]]:
    """Return a mapping of doc path -> change entries that mention it."""
    if not pending:
        return {}
    subjects = {e.subject_id for e in pending if e.subject_id}
    matches = _ripgrep_files_with(subjects, workspace)
    if matches is None:
        matches = _python_walk_files_with(subjects, workspace)

    result: dict[Path, list[ChangeEntry]] = {}
    for path in matches:
        if path.suffix != ".md":
            continue  # refinement only touches markdown for now
        try:
            text = path.read_text()
        except OSError:
            continue
        relevant = [e for e in pending if e.subject_id and e.subject_id in text]
        if relevant:
            result[path] = relevant
    return result


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _split_frontmatter(text: str) -> tuple[str, list[str]]:
    body = strip_frontmatter(text)
    if body == text:
        frontmatter = ""
    else:
        frontmatter = text[: len(text) - len(body)]
    return frontmatter, body.splitlines()


def build_doc_view(
    path: Path,
    related: list[ChangeEntry],
    *,
    window_lines: int,
) -> Optional[DocView]:
    """Slice a doc into scoped windows around the matches for ``related``."""
    try:
        text = path.read_text()
    except OSError:
        return None

    frontmatter, body_lines = _split_frontmatter(text)
    toc = [line for line in body_lines if _HEADING_RE.match(line)]

    subjects = {e.subject_id for e in related if e.subject_id}
    if not subjects:
        return None

    # Find every line index (0-based) containing a subject id.
    hit_indices: list[tuple[int, list[str]]] = []
    for idx, line in enumerate(body_lines):
        matched = [s for s in subjects if s in line]
        if matched:
            hit_indices.append((idx, matched))
    if not hit_indices:
        return None

    # Merge overlapping ±window ranges into windows.
    raw_ranges: list[tuple[int, int, list[str]]] = []
    for idx, matched in hit_indices:
        start = max(0, idx - window_lines)
        end = min(len(body_lines) - 1, idx + window_lines)
        raw_ranges.append((start, end, matched))
    raw_ranges.sort(key=lambda r: (r[0], r[1]))

    merged: list[tuple[int, int, list[str]]] = []
    for start, end, matched in raw_ranges:
        if merged and start <= merged[-1][1] + 1:
            prev_start, prev_end, prev_matched = merged[-1]
            merged[-1] = (
                prev_start,
                max(prev_end, end),
                sorted(set(prev_matched) | set(matched)),
            )
        else:
            merged.append((start, end, matched))

    windows = [
        Window(
            anchor=i + 1,
            start_line=start + 1,
            end_line=end + 1,
            text="\n".join(body_lines[start : end + 1]),
            matched_subjects=matched,
        )
        for i, (start, end, matched) in enumerate(merged)
    ]
    return DocView(
        path=path,
        frontmatter=frontmatter,
        body_lines=body_lines,
        toc=toc,
        windows=windows,
        related_changes=related,
    )


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


REFINER_SYSTEM_PROMPT = """\
You are a memory refinement engine for an autonomous agent. Your job is to
revise the agent's existing learnings in light of a small set of changes
(corrections, guidance updates, etc.). Your edits MUST be minimal and grounded
in the change entries — do not invent unrelated revisions.

You will be given:
- the path of a single workspace document
- its YAML frontmatter (read-only context)
- a TOC of its top-level headings
- one or more "windows" — short slices of the document around references the
  changes mention. Each window has an `anchor` integer and a `text` block.
  Only the text inside windows is mutable. Content outside windows is unseen
  and unchanged.
- the change entries that triggered this doc's inclusion

Your job: for each window that needs revision, produce a `window_replacements`
entry with the same `anchor` and a `new_text` that should replace the window
text verbatim. If a window does not need editing, omit it. If a memory id was
mentioned in a correction-type change and should be deleted from vector
storage, include it in `memory_deletes`.

Rules:
- new_text MUST preserve unrelated lines from the original window verbatim.
  Edit ONLY what the changes call for.
- Preserve markdown link forms — never strip [label](type:UUID) references
  unless the change explicitly says the asset is gone.
- If you have nothing to revise for a doc, return an empty
  `window_replacements` list and write a one-line `summary`.
- Output ONLY a single JSON object with no markdown fences:

{
  "window_replacements": [
    {"anchor": 1, "new_text": "..."}
  ],
  "memory_deletes": ["<memory-uuid>"],
  "summary": "one-line summary of the edits"
}
"""


def _format_change(entry: ChangeEntry) -> str:
    payload = json.dumps(entry.payload, sort_keys=True) if entry.payload else "{}"
    return (
        f"- [{entry.kind.value}] subject={entry.subject_type or '?'}:{entry.subject_id} "
        f"team={entry.team_id or '-'} occurred_at={entry.occurred_at}\n"
        f"  payload={payload}"
    )


def _format_doc_user_message(view: DocView, soul_excerpt: str) -> str:
    parts: list[str] = []
    parts.append(f"## Document\n`{view.path}`")
    if view.frontmatter:
        parts.append(f"## Frontmatter (read-only)\n```\n{view.frontmatter.strip()}\n```")
    if view.toc:
        toc_block = "\n".join(view.toc[:30])
        parts.append(f"## Headings (TOC)\n{toc_block}")
    parts.append("## Windows")
    for w in view.windows:
        parts.append(
            f"### anchor={w.anchor} (lines {w.start_line}-{w.end_line}, "
            f"matched: {', '.join(w.matched_subjects) or '-'})\n"
            f"```\n{w.text}\n```"
        )
    parts.append("## Changes that triggered this doc")
    parts.append("\n".join(_format_change(c) for c in view.related_changes))
    if soul_excerpt.strip():
        parts.append(f"## Agent SOUL (excerpt, for tone/voice only)\n{soul_excerpt[:2000]}")
    return "\n\n".join(parts)


def _parse_llm_response(raw: str) -> tuple[list[dict], list[str], str]:
    """Tolerantly parse the refiner's JSON envelope."""
    data = parse_llm_json(raw, expect=dict)
    if not isinstance(data, dict):
        raise ValueError("Refiner LLM returned non-JSON or non-object")
    replacements = data.get("window_replacements") or []
    memory_deletes = data.get("memory_deletes") or []
    summary = str(data.get("summary") or "").strip()
    if not isinstance(replacements, list) or not isinstance(memory_deletes, list):
        raise ValueError("window_replacements/memory_deletes must be lists")
    return replacements, [str(m) for m in memory_deletes if m], summary


def call_refiner_llm(model, view: DocView, soul_excerpt: str = "") -> tuple[list[dict], list[str], str, str, str]:
    """Return (replacements, mem_deletes, doc_summary, user_msg, raw_response)."""
    user_msg = _format_doc_user_message(view, soul_excerpt)
    result = model(
        [
            {"role": "system", "content": REFINER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = result.content if hasattr(result, "content") else str(result)
    replacements, mem_deletes, doc_summary = _parse_llm_response(str(raw))
    return replacements, mem_deletes, doc_summary, user_msg, str(raw)


# ---------------------------------------------------------------------------
# Apply edits
# ---------------------------------------------------------------------------


def _apply_window_replacements(
    view: DocView, replacements: list[dict]
) -> tuple[str, int]:
    """Apply replacements to a copy of the doc body. Returns (new_text, count)."""
    by_anchor = {w.anchor: w for w in view.windows}
    valid_edits: list[tuple[int, int, str]] = []  # (start_line, end_line, new_text)
    for rep in replacements:
        anchor = rep.get("anchor")
        new_text = rep.get("new_text")
        if not isinstance(anchor, int) or not isinstance(new_text, str):
            continue
        window = by_anchor.get(anchor)
        if not window:
            continue
        valid_edits.append((window.start_line, window.end_line, new_text))

    if not valid_edits:
        return "", 0

    # Apply in reverse order so earlier edits don't shift later line indexes.
    body_lines = list(view.body_lines)
    for start, end, new_text in sorted(valid_edits, key=lambda r: r[0], reverse=True):
        new_lines = new_text.splitlines() or [""]
        body_lines[start - 1 : end] = new_lines

    new_body = "\n".join(body_lines)
    if not new_body.endswith("\n"):
        new_body += "\n"
    new_text = view.frontmatter + new_body if view.frontmatter else new_body
    new_text = set_frontmatter_timestamp(new_text, datetime.now(timezone.utc))
    return new_text, len(valid_edits)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_refinement(
    *,
    agent: "OuroAgent",
    queue: Optional[ChangeSetQueue] = None,
    model=None,
    max_changes_per_pass: int = 25,
    max_docs_per_pass: int = 15,
    window_lines: int = 20,
) -> RefinementSummary:
    """Drain pending change-set entries and apply refinements."""
    summary = RefinementSummary()
    queue = queue or _default_queue(agent)
    pending = queue.pending(limit=max_changes_per_pass)
    summary.pending_seen = len(pending)
    if not pending:
        logger.info("Refinement: no pending changes")
        return summary
    if model is None:
        model = _build_default_model(agent)
        if model is None:
            summary.errors.append("No model available for refinement")
            return summary

    affected = collect_affected_docs(agent.config.agent.workspace, pending)
    if not affected:
        logger.info(
            "Refinement: %d pending change(s) but no matching workspace docs",
            len(pending),
        )
        # Still mark as applied so we don't loop forever on unrelated changes.
        queue.mark_applied([c.id for c in pending], summary="no matching docs")
        summary.queue_marked_applied = len(pending)
        return summary

    soul = getattr(agent, "soul", "") or ""
    docs = list(affected.items())[:max_docs_per_pass]
    summary.docs_inspected = len(docs)

    applied_change_ids: set[str] = set()
    aggregate_memory_deletes: list[str] = []

    for path, related in docs:
        view = build_doc_view(path, related, window_lines=window_lines)
        if not view or not view.windows:
            continue
        try:
            replacements, mem_deletes, doc_summary, user_msg, raw = call_refiner_llm(
                model, view, soul
            )
        except Exception as exc:
            logger.warning("Refiner LLM failed on %s: %s", path, exc)
            summary.errors.append(f"{path}: {exc}")
            continue

        summary.llm_calls.append(
            {
                "phase": "refinement",
                "target": str(path),
                "system": REFINER_SYSTEM_PROMPT[:1500],
                "user": user_msg[:1500],
                "response": raw[:1500],
                "system_chars": len(REFINER_SYSTEM_PROMPT),
                "user_chars": len(user_msg),
                "response_chars": len(raw),
            }
        )

        new_content, applied = _apply_window_replacements(view, replacements)
        if applied:
            try:
                path.write_text(new_content)
                summary.files_rewritten.append(str(path))
                summary.windows_applied += applied
            except OSError as exc:
                summary.errors.append(f"{path}: write failed: {exc}")
                continue

        aggregate_memory_deletes.extend(mem_deletes)
        if doc_summary:
            summary.per_doc_summaries.append(f"{path.name}: {doc_summary}")
        applied_change_ids.update(c.id for c in related)

    # Apply mem0 deletes (deduped). The refiner is the authority on these.
    deduped_mem = sorted({m for m in aggregate_memory_deletes if m})
    for mid in deduped_mem:
        try:
            agent.memory.delete(mid)
            summary.memory_deletes += 1
        except Exception as exc:
            logger.warning("Refinement: memory delete failed for %s: %s", mid, exc)

    if applied_change_ids:
        marked = queue.mark_applied(
            sorted(applied_change_ids),
            summary="; ".join(summary.per_doc_summaries)[:500],
        )
        summary.queue_marked_applied = marked

    logger.info(
        "Refinement complete: %d window edits across %d file(s), %d mem0 deletes, "
        "%d/%d queue entries applied",
        summary.windows_applied,
        len(summary.files_rewritten),
        summary.memory_deletes,
        summary.queue_marked_applied,
        summary.pending_seen,
    )
    return summary


def _default_queue(agent: "OuroAgent") -> ChangeSetQueue:
    from ..tools.workspace_paths import protected_data

    path = protected_data(agent.config.agent.workspace) / "change_queue.jsonl"
    return ChangeSetQueue(path)


def _build_default_model(agent: "OuroAgent"):
    """Build the cheap model the scheduler / script uses by default."""
    cfg = agent.config
    refinement_cfg = getattr(cfg, "refinement", None)
    model_id: Optional[str] = None
    if refinement_cfg and getattr(refinement_cfg, "model", None):
        model_id = refinement_cfg.model
    if not model_id:
        model_id = agent._utility_model_id()
    try:
        return agent._build_model(model_id, role="refinement")
    except Exception as exc:
        logger.warning("Refinement: failed to build model %s: %s", model_id, exc)
        return None


__all__ = [
    "DocView",
    "RefinementSummary",
    "Window",
    "build_doc_view",
    "call_refiner_llm",
    "collect_affected_docs",
    "run_refinement",
]
