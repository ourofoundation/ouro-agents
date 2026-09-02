"""Privileged, bounded tools exposed only during agentic dream runs."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smolagents import tool

from ..memory.dream import (
    DreamPlan,
    compact_memory_md as _compact_memory_md,
    decay_memory_strength,
    promote_log_entries as _promote_log_entries,
)
from .workspace_layout import check_workspace_write
from .workspace_paths import protected_data


@dataclass
class DreamToolState:
    run_id: str
    dry_run: bool
    context: dict[str, Any]
    max_changes: int
    changes: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    friction_resolved: list[str] = field(default_factory=list)
    report_path: str = ""
    report_markdown: str = ""
    expectations: list[str] = field(default_factory=list)
    decay_candidates: dict[str, dict[str, Any]] = field(default_factory=dict)


def _safe_workspace_path(workspace: Path, raw: str) -> Path:
    workspace = workspace.resolve()
    target = (workspace / raw).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError(f"Path escapes workspace: {raw}") from exc
    return target


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _declares_load_always(content: str) -> bool:
    if not content.startswith("---"):
        return False
    end = content.find("\n---", 3)
    if end < 0:
        return False
    return bool(
        re.search(
            r"(?mi)^\s*load\s*:\s*['\"]?always['\"]?\s*$",
            content[3:end],
        )
    )


def _record_change(
    state: DreamToolState,
    *,
    path: str,
    kind: str,
    why: str,
) -> None:
    item = {"path": path, "kind": kind, "why": why}
    if item not in state.changes:
        state.changes.append(item)


def make_dream_tools(
    *,
    workspace: Path,
    agent_name: str,
    backend,
    memory_config,
    maintenance_model,
    doc_store,
    run_id: str,
    context: dict[str, Any],
    friction_queue,
    dry_run: bool,
    max_changes: int,
) -> tuple[list, DreamToolState]:
    """Build the dream-only tool surface and its mutable audit state."""
    workspace = workspace.resolve()
    state = DreamToolState(
        run_id=run_id,
        dry_run=dry_run,
        context=context,
        max_changes=max_changes,
    )

    @tool
    def dream_context() -> str:
        """Return the evidence window assembled for this dream run."""
        return _json(state.context)

    @tool
    def read_workspace_doc(path: str) -> str:
        """Read a workspace document by relative path.

        Args:
            path: Relative path such as NOTES.md or skills/lessons-example.md.
        """
        return _safe_workspace_path(workspace, path).read_text()

    @tool
    def write_workspace_doc(path: str, content: str, why: str) -> str:
        """Replace an allowed self-improvement document.

        Args:
            path: Relative path in the dream writable tier.
            content: Complete new document content.
            why: Evidence-backed reason for the change.
        """
        target = check_workspace_write(
            _safe_workspace_path(workspace, path), workspace, is_dir=False
        )
        if path.startswith("skills/") and _declares_load_always(content):
            raise PermissionError(
                "Dream write tier: skills with `load: always` are proposal-only. "
                "Use `propose_change`."
            )
        _record_change(state, path=path, kind="write", why=why)
        if state.dry_run:
            return _json({"status": "planned", "path": path, "chars": len(content)})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return _json({"status": "written", "path": path, "chars": len(content)})

    @tool
    def append_workspace_doc(path: str, text: str, why: str) -> str:
        """Append evidence-backed text to an allowed workspace document.

        Args:
            path: Relative path in the dream writable tier.
            text: Text to append.
            why: Evidence-backed reason for the change.
        """
        target = check_workspace_write(
            _safe_workspace_path(workspace, path), workspace, is_dir=False
        )
        if (
            path.startswith("skills/")
            and not target.exists()
            and _declares_load_always(text)
        ):
            raise PermissionError(
                "Dream write tier: skills with `load: always` are proposal-only. "
                "Use `propose_change`."
            )
        _record_change(state, path=path, kind="append", why=why)
        if state.dry_run:
            return _json({"status": "planned", "path": path, "chars": len(text)})
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(text)
        return _json({"status": "appended", "path": path, "chars": len(text)})

    @tool
    def propose_change(target: str, rationale: str, proposed_content_or_diff: str) -> str:
        """Propose, but do not apply, an identity-level change.

        Args:
            target: Proposal target, such as SOUL.md or an always-loaded skill.
            rationale: Evidence and expected behavioral effect.
            proposed_content_or_diff: Suggested replacement content or diff.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-") or "change"
        path = protected_data(workspace) / "dream_proposals" / f"{timestamp}-{slug}.md"
        body = (
            f"# Dream proposal: {target}\n\n"
            f"Run: `{run_id}`\n\n"
            f"## Rationale\n\n{rationale.strip()}\n\n"
            f"## Proposed change\n\n{proposed_content_or_diff.rstrip()}\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        proposal = {
            "target": target,
            "rationale": rationale,
            "path": str(path),
        }
        state.proposals.append(proposal)
        return _json({"status": "proposed", **proposal})

    @tool
    def list_friction(status: str = "pending") -> str:
        """List process-friction observations awaiting dream review.

        Args:
            status: Queue status to list, normally pending.
        """
        try:
            entries = (
                friction_queue.pending()
                if status == "pending"
                else friction_queue.list(status)
            )
        except AttributeError:
            entries = friction_queue.pending()
        except Exception as exc:
            return _json({"status": "error", "error": str(exc)})
        entries = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in entries
        ]
        return _json({"status": status, "count": len(entries), "entries": entries})

    @tool
    def resolve_friction(ids: list[str], disposition: str, note: str) -> str:
        """Resolve reviewed friction notes.

        Args:
            ids: Friction entry identifiers.
            disposition: fixed, learned, dismissed, or deferred.
            note: Evidence-backed resolution note.
        """
        if state.dry_run:
            return _json({"status": "planned", "ids": ids})
        resolved = friction_queue.resolve(
            ids,
            dream_run_id=run_id,
            disposition=disposition,
            note=note,
        )
        state.friction_resolved.extend(str(item) for item in ids)
        return _json({"status": "resolved", "count": resolved, "ids": ids})

    @tool
    def memory_decay_candidates() -> str:
        """Preview old, unused memories eligible for weakening or deletion."""
        try:
            memories = backend.get_all(agent_id=agent_name, limit=300)
            copies = [
                item.model_copy(deep=True)
                if hasattr(item, "model_copy")
                else deepcopy(item)
                for item in memories
            ]
            plan = DreamPlan(
                scope="agentic",
                agent_id=agent_name,
                team_id=None,
                rhythm=memory_config.rhythm,
                period="",
                dry_run=True,
                mode="agentic",
                run_id=run_id,
            )
            decay_memory_strength(
                backend,
                agent_name,
                memory_config,
                all_memories=copies,
                dry_run=True,
                plan=plan,
            )
            candidates = []
            for operation in plan.operations:
                if operation.kind != "strength_decay" or not operation.memory_id:
                    continue
                item = {
                    "memory_id": operation.memory_id,
                    "text": operation.excerpt,
                    "reason": operation.reason,
                    "old_metadata": operation.old_metadata,
                    "new_metadata": operation.new_metadata,
                }
                state.decay_candidates[operation.memory_id] = item
                candidates.append(item)
            return _json({"count": len(candidates), "candidates": candidates})
        except Exception as exc:
            return _json({"status": "error", "error": str(exc)})

    @tool
    def apply_memory_decay(memory_ids: list[str], reason: str) -> str:
        """Apply previously previewed decay decisions to selected memories.

        Args:
            memory_ids: IDs returned by memory_decay_candidates.
            reason: Why these candidates are safe to weaken or delete.
        """
        unknown = [mid for mid in memory_ids if mid not in state.decay_candidates]
        if unknown:
            return _json({"status": "error", "unknown_ids": unknown})
        _record_change(
            state,
            path="vector-memory",
            kind="decay",
            why=reason,
        )
        if state.dry_run:
            return _json({"status": "planned", "memory_ids": memory_ids})
        deleted = 0
        updated = 0
        for memory_id in memory_ids:
            metadata = dict(state.decay_candidates[memory_id]["new_metadata"])
            if metadata.pop("deleted", False):
                backend.delete(memory_id)
                deleted += 1
            else:
                backend.update_metadata(memory_id, metadata)
                updated += 1
        return _json({"status": "applied", "updated": updated, "deleted": deleted})

    @tool
    def compact_memory_md() -> str:
        """Compact working memory when it is over the configured token budget."""
        changed = _compact_memory_md(
            workspace,
            memory_config,
            maintenance_model,
            doc_store=doc_store,
            agent_name=agent_name,
            dry_run=state.dry_run,
        )
        if changed:
            _record_change(
                state,
                path="MEMORY.md",
                kind="compact",
                why="Working memory exceeded its configured token budget.",
            )
        return _json({"status": "planned" if state.dry_run else "applied", "changed": changed})

    @tool
    def promote_log_entries() -> str:
        """Promote durable entries from the previous period log on demand."""
        count = _promote_log_entries(
            workspace,
            maintenance_model,
            doc_store=doc_store,
            agent_name=agent_name,
            dry_run=state.dry_run,
        )
        if count:
            _record_change(
                state,
                path="MEMORY.md",
                kind="promote",
                why="Selected durable knowledge from the previous period log.",
            )
        return _json({"status": "planned" if state.dry_run else "applied", "promoted": count})

    @tool
    def write_dream_report(
        markdown: str,
        changes: list[dict],
        expectations: list[str],
    ) -> str:
        """Finalize the dream journal report.

        Args:
            markdown: Concise report with evidence, grading, changes, and proposals.
            changes: List of path/kind/why change records.
            expectations: Observable expectations for the next dream window.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = protected_data(workspace) / "dreams" / f"{timestamp}-{run_id}.md"
        merged = state.changes[:]
        for change in changes:
            if isinstance(change, dict) and change not in merged:
                merged.append(change)
        warning = ""
        if len(merged) > state.max_changes:
            warning = (
                f"\n\n> Warning: {len(merged)} changes exceed the configured "
                f"soft cap of {state.max_changes}.\n"
            )
        content = (
            f"---\nrun_id: {run_id}\ndry_run: {str(state.dry_run).lower()}\n"
            f"created_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
            f"{markdown.rstrip()}{warning}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        state.changes = merged
        state.expectations = [str(item) for item in expectations]
        state.report_path = str(path)
        state.report_markdown = markdown
        return _json(
            {
                "status": "complete",
                "report_path": str(path),
                "changes": len(merged),
                "warning": warning.strip(),
            }
        )

    return [
        dream_context,
        read_workspace_doc,
        write_workspace_doc,
        append_workspace_doc,
        propose_change,
        list_friction,
        resolve_friction,
        memory_decay_candidates,
        apply_memory_decay,
        compact_memory_md,
        promote_log_entries,
        write_dream_report,
    ], state
