"""Dream orchestration for evidence-driven self-improvement."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..modes.profiles import RunMode
from ..tools.workspace_paths import protected_data
from ..uuid_v7 import uuid7_str
from .dream import (
    compact_memory_md,
    read_dream_status,
    run_refinement_phase,
    write_dream_status,
)
from .dream_git import DreamSnapshot, diff_snapshots, find_git_root, snapshot
from .naming import period_key, period_key_offset

logger = logging.getLogger(__name__)


def _read_recent_reports(workspace: Path, limit: int) -> list[dict[str, str]]:
    directory = protected_data(workspace) / "dreams"
    if not directory.is_dir():
        return []
    reports = []
    for path in sorted(directory.glob("*.md"), reverse=True)[:limit]:
        try:
            reports.append({"path": str(path), "content": path.read_text()[:8000]})
        except OSError:
            continue
    return reports


def _skill_index(workspace: Path) -> list[dict[str, Any]]:
    skills = workspace / "skills"
    if not skills.is_dir():
        return []
    output = []
    for path in sorted(skills.glob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        output.append(
            {
                "path": path.relative_to(workspace).as_posix(),
                "bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            }
        )
    return output


def _read_working_memory(agent) -> str:
    store = agent.doc_store
    if store is None:
        return ""
    try:
        return store.read(store.memory_name(agent.config.agent.name))[:12000]
    except Exception:
        return ""


def _read_period_logs(agent) -> list[dict[str, str]]:
    store = agent.doc_store
    if store is None:
        return []
    rhythm = agent.config.memory.rhythm
    output = []
    for offset in (0, -1):
        key = period_key(rhythm) if offset == 0 else period_key_offset(rhythm, offset)
        try:
            name = store.log_name(agent.config.agent.name, key)
            content = store.read(name)
        except Exception:
            continue
        if content.strip():
            output.append({"period": key, "content": content[:8000]})
    return output


def _run_window(agent, since: str | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = agent._run_log.query_runs(since=since, limit=500)
    rows = [
        row
        for row in rows
        if row.get("mode") not in {"dream", "plan"}
        and not row.get("parent_run_id")
    ]
    counts = Counter(str(row.get("mode") or "unknown") for row in rows)
    summaries = []
    for row in rows[:50]:
        summaries.append(
            {
                "run_id": row.get("run_id"),
                "started_at": row.get("started_at"),
                "mode": row.get("mode"),
                "status": row.get("status"),
                "num_steps": row.get("num_steps"),
                "task": str(row.get("task") or "")[:300],
                "result": str(row.get("result") or "")[:300],
                "error": str(row.get("error_message") or "")[:300],
            }
        )
    return summaries, dict(counts)


def build_dream_context(agent, friction_queue) -> dict[str, Any]:
    """Assemble the bounded evidence window supplied to the dream agent."""
    workspace = Path(agent.config.agent.workspace)
    cfg = agent.config.dream
    status = read_dream_status(workspace) or {}
    since = status.get("last_dream_at") or status.get("completed_at")
    runs, counts = _run_window(agent, since)
    try:
        friction = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in friction_queue.pending()
        ]
    except Exception:
        friction = []
    try:
        from ..modes.outcomes import build_outcome_evidence_context

        outcomes = build_outcome_evidence_context(agent, limit=8)
    except Exception as exc:
        logger.debug("Dream outcome digest unavailable: %s", exc)
        outcomes = ""
    return {
        "policy": {
            "max_changes": cfg.max_changes,
            "writable": list(cfg.writable),
            "proposal_only": list(cfg.proposal_only),
            "dry_run": bool(cfg.dry_run),
        },
        "window": {
            "since": since,
            "until": datetime.now(timezone.utc).isoformat(),
            "new_runs": len(runs),
            "runs_by_mode": counts,
        },
        "runs": runs,
        "friction": friction,
        "outcome_evidence": outcomes,
        "working_memory": _read_working_memory(agent),
        "period_logs": _read_period_logs(agent),
        "skills": _skill_index(workspace),
        "previous_dreams": _read_recent_reports(workspace, cfg.journal_lookback),
        "pending_proposals": [
            str(path)
            for path in sorted(
                (protected_data(workspace) / "dream_proposals").glob("*.md")
            )
        ][-20:]
        if (protected_data(workspace) / "dream_proposals").is_dir()
        else [],
    }


def _current_git_snapshot(workspace: Path) -> DreamSnapshot | None:
    root = find_git_root(workspace)
    if root is None:
        return None
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return DreamSnapshot(kind="git", ref=result.stdout.strip(), changed=False)


def _write_fallback_report(workspace: Path, run_id: str, dry_run: bool, result: str) -> str:
    path = protected_data(workspace) / "dreams" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"run_id: {run_id}\ndry_run: {str(dry_run).lower()}\n"
        f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
        "---\n\n# Dream report\n\n"
        "The run ended without calling `write_dream_report`.\n\n"
        f"## Final result\n\n{result}\n"
    )
    return str(path)


def _write_dream_audit(workspace: Path, payload: dict[str, Any]) -> str:
    directory = protected_data(workspace) / "dream_runs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"_dream_{payload['run_id']}.json"
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return str(path)


def run_dream(agent, *, dry_run: bool = False, mode: str = "manual") -> dict[str, Any]:
    """Run one agent-wide, evidence-driven dream through ``agent.run``."""
    from ..tools.dream_tools import make_dream_tools

    try:
        from .friction import FrictionQueue

        friction_queue = FrictionQueue.for_workspace(
            Path(agent.config.agent.workspace)
        )
    except Exception:
        # This fallback keeps dream operable while a corrupt queue is repaired.
        class _EmptyQueue:
            def pending(self):
                return []

            def resolve(self, *args, **kwargs):
                return 0

        friction_queue = _EmptyQueue()

    workspace = Path(agent.config.agent.workspace).resolve()
    cfg = agent.config.dream
    effective_dry_run = bool(dry_run or cfg.dry_run)
    run_id = uuid7_str()
    context = build_dream_context(agent, friction_queue)
    context["policy"]["dry_run"] = effective_dry_run
    started_at = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []

    try:
        before = (
            _current_git_snapshot(workspace)
            if effective_dry_run
            else snapshot(
                workspace,
                "pre-dream snapshot",
                agent_name=agent.config.agent.name,
            )
        )
    except Exception as exc:
        warnings.append(f"pre_snapshot_failed: {exc}")
        before = _current_git_snapshot(workspace)

    maintenance_model = agent._build_model(
        agent._utility_model_id(),
        role="utility",
    )
    refinement = run_refinement_phase(agent, dry_run=effective_dry_run)
    compacted = compact_memory_md(
        workspace,
        agent.config.memory,
        maintenance_model,
        doc_store=agent.doc_store,
        agent_name=agent.config.agent.name,
        dry_run=effective_dry_run,
    )
    tools, tool_state = make_dream_tools(
        workspace=workspace,
        agent_name=agent.config.agent.name,
        backend=agent.memory,
        memory_config=agent.config.memory,
        maintenance_model=maintenance_model,
        doc_store=agent.doc_store,
        run_id=run_id,
        context=context,
        friction_queue=friction_queue,
        dry_run=effective_dry_run,
        max_changes=cfg.max_changes,
    )
    task = (
        "Review and improve your own operating system using the evidence below. "
        "Inspect detailed runs with recall_runs/get_run_detail when useful. "
        "Make only evidence-backed changes, resolve the friction you actually "
        "handled, and finish by calling write_dream_report. Changing nothing "
        "is valid.\n\nDREAM CONTEXT\n"
        + json.dumps(context, indent=2, default=str)
    )
    model_id = cfg.model or agent._model_id_for_role("dream") or agent.config.agent.model
    dream_model = agent._build_model(model_id, role="dream")

    try:
        from ..tools.workspace_layout import dream_write_scope

        write_scope = dream_write_scope(
            workspace,
            writable=cfg.writable,
            proposal_only=cfg.proposal_only,
        )
    except ImportError:
        write_scope = nullcontext()

    result = ""
    error = ""
    try:
        with write_scope:
            result = asyncio.run(
                agent.run(
                    task,
                    model_override=dream_model,
                    mode=RunMode.DREAM,
                    skip_memory=True,
                    allowed_servers=cfg.servers,
                    extra_tools=tools,
                    run_id_override=run_id,
                    max_steps_override=cfg.max_steps,
                )
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Agentic dream failed")

    if not tool_state.report_path:
        tool_state.report_path = _write_fallback_report(
            workspace, run_id, effective_dry_run, result or error
        )

    try:
        after = (
            _current_git_snapshot(workspace)
            if effective_dry_run
            else snapshot(
                workspace,
                "apply dream",
                agent_name=agent.config.agent.name,
            )
        )
    except Exception as exc:
        warnings.append(f"post_snapshot_failed: {exc}")
        after = _current_git_snapshot(workspace)
    diff = (
        diff_snapshots(workspace, before, after)
        if before is not None and after is not None
        else ""
    )
    completed_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "run_id": run_id,
        "mode": mode,
        "dry_run": effective_dry_run,
        "window": context["window"],
        "refinement": refinement,
        "compacted": compacted,
        "changes": tool_state.changes,
        "proposals": tool_state.proposals,
        "friction_resolved": tool_state.friction_resolved,
        "report_path": tool_state.report_path,
        "expectations": tool_state.expectations,
        "error": error or None,
        "started_at": started_at,
        "completed_at": completed_at,
        "git_before": before.ref if before else None,
        "git_after": after.ref if after else None,
        "diff": diff,
        "warnings": warnings,
    }
    if error:
        summary["failures"] = [error]
    summary["audit_log"] = _write_dream_audit(workspace, summary)
    write_dream_status(
        workspace,
        period_key(agent.config.memory.rhythm),
        {"dream": summary},
    )
    try:
        from ..skills import invalidate_skill_cache

        invalidate_skill_cache(workspace)
    except Exception:
        logger.debug("Failed to invalidate skill cache", exc_info=True)
    return summary
