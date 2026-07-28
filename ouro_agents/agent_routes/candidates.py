"""Mine repeated tool-call n-grams from the run log as route candidates."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .mcp_map import annotate_signature, normalize_tool_name
from .registry import candidates_path, load_published_registry

logger = logging.getLogger(__name__)

# Framework / meta tools that pollute route signatures.
EXCLUDED_TOOLS = frozenset(
    {
        "load_skill",
        "load_tool",
        "memory_recall",
        "remember",
        "update_memory",
        "forget",
        "read_context",
        "run_route",
        "publish_route",
        "unpublish_route",
        "delegate",
        "final_answer",
        "ask_controller",
        "run_python",
        "run_shell",
        "list_runs",
        "get_run",
        "schedule_task",
        "list_scheduled_tasks",
        "cancel_scheduled_task",
    }
)


@dataclass
class RouteCandidate:
    signature: list[str]
    run_count: int
    example_args: list[list[dict[str, Any]]] = field(default_factory=list)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None

    @property
    def key(self) -> str:
        return " -> ".join(self.signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "run_count": self.run_count,
            "example_args": self.example_args,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "annotated": annotate_signature(self.signature),
        }


def _truncate_args(args: Any, *, limit: int = 400) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {"_raw": str(args)[:limit]}
    text = json.dumps(args, default=str)
    if len(text) <= limit:
        return args
    return {"_truncated": text[:limit] + "…"}


def _tool_sequence(steps: list[dict]) -> tuple[list[str], list[dict[str, Any]]]:
    names: list[str] = []
    args_list: list[dict[str, Any]] = []
    for step in steps:
        raw = step.get("tool_calls_json") or step.get("tool_calls")
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                calls = json.loads(raw)
            except json.JSONDecodeError:
                continue
        else:
            calls = raw
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = normalize_tool_name(call.get("name") or "")
            if not name or name in EXCLUDED_TOOLS:
                continue
            names.append(name)
            args_list.append(_truncate_args(call.get("args") or {}))
    return names, args_list


def _contains_as_subsequence(haystack: list[str], needle: list[str]) -> bool:
    if len(needle) > len(haystack):
        return False
    n = len(needle)
    for i in range(len(haystack) - n + 1):
        if haystack[i : i + n] == needle:
            return True
    return False


def mine_route_candidates(
    run_log: Any,
    *,
    since_iso: Optional[str] = None,
    min_len: int = 3,
    max_len: int = 5,
    min_runs: int = 3,
    limit_runs: int = 200,
) -> list[RouteCandidate]:
    """Return repeated tool-call n-grams across successful runs."""
    if run_log is None or not getattr(run_log, "enabled", True):
        return []

    runs = run_log.query_runs(status="success", since=since_iso, limit=limit_runs)
    # signature_tuple -> set(run_id), first/last, examples
    seen_runs: dict[tuple[str, ...], set[str]] = defaultdict(set)
    first_seen: dict[tuple[str, ...], str] = {}
    last_seen: dict[tuple[str, ...], str] = {}
    examples: dict[tuple[str, ...], list[list[dict[str, Any]]]] = defaultdict(list)

    for run in runs:
        run_id = str(run.get("run_id") or "")
        started = str(run.get("started_at") or "")
        steps = run_log.get_run_steps(run_id)
        names, args_list = _tool_sequence(steps)
        if len(names) < min_len:
            continue

        # Deduplicate n-grams within a single run.
        run_ngrams: set[tuple[str, ...]] = set()
        for length in range(min_len, min(max_len, len(names)) + 1):
            for i in range(len(names) - length + 1):
                sig = tuple(names[i : i + length])
                if sig in run_ngrams:
                    continue
                run_ngrams.add(sig)
                seen_runs[sig].add(run_id)
                if sig not in first_seen or (started and started < first_seen[sig]):
                    first_seen[sig] = started
                if sig not in last_seen or (started and started > last_seen[sig]):
                    last_seen[sig] = started
                if len(examples[sig]) < 2:
                    examples[sig].append(args_list[i : i + length])

    candidates: list[RouteCandidate] = []
    for sig, run_ids in seen_runs.items():
        if len(run_ids) < min_runs:
            continue
        candidates.append(
            RouteCandidate(
                signature=list(sig),
                run_count=len(run_ids),
                example_args=examples.get(sig, []),
                first_seen=first_seen.get(sig),
                last_seen=last_seen.get(sig),
            )
        )

    # Prefer maximal sequences: drop shorter n-grams contained in a longer one
    # with the same run count.
    candidates.sort(key=lambda c: (-len(c.signature), -c.run_count, c.key))
    kept: list[RouteCandidate] = []
    for cand in candidates:
        dominated = False
        for longer in kept:
            if len(longer.signature) <= len(cand.signature):
                continue
            if longer.run_count != cand.run_count:
                continue
            if _contains_as_subsequence(longer.signature, cand.signature):
                dominated = True
                break
        if not dominated:
            kept.append(cand)
    kept.sort(key=lambda c: (-c.run_count, -len(c.signature), c.key))
    return kept


def load_candidates_state(workspace: Path) -> dict[str, Any]:
    path = candidates_path(workspace)
    if not path.is_file():
        return {"signatures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"signatures": {}}
        data.setdefault("signatures", {})
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load candidates state %s: %s", path, exc)
        return {"signatures": {}}


def save_candidates_state(workspace: Path, state: dict[str, Any]) -> None:
    from ..tools.workspace_paths import ensure_protected_dir
    from .registry import published_routes_root

    ensure_protected_dir(workspace)
    root = published_routes_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    path = candidates_path(workspace)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mined_from_signatures(workspace: Path) -> set[str]:
    """Signatures already captured as ``mined_from`` on draft/published routes."""
    from .manifest import load_route_manifests

    keys: set[str] = set()
    for manifests in (
        load_route_manifests(workspace, published_only=False),
        load_route_manifests(workspace, published_only=True),
    ):
        for manifest in manifests.values():
            if manifest.mined_from:
                keys.add(" -> ".join(manifest.mined_from))
    return keys


def filter_new_candidates(
    workspace: Path, candidates: list[RouteCandidate]
) -> tuple[list[RouteCandidate], list[RouteCandidate]]:
    """Split into (new_to_suggest, already_known)."""
    state = load_candidates_state(workspace)
    known = set(state.get("signatures", {}).keys())
    known |= _mined_from_signatures(workspace)
    fresh: list[RouteCandidate] = []
    known_list: list[RouteCandidate] = []
    for cand in candidates:
        if cand.key in known:
            known_list.append(cand)
        else:
            fresh.append(cand)
    return fresh, known_list


def mark_candidates_suggested(
    workspace: Path, candidates: list[RouteCandidate]
) -> None:
    state = load_candidates_state(workspace)
    sigs = state.setdefault("signatures", {})
    now = datetime.now(timezone.utc).isoformat()
    for cand in candidates:
        entry = sigs.get(cand.key) or {}
        entry.update(
            {
                "suggested_at": now,
                "run_count": cand.run_count,
                "signature": cand.signature,
                "dismissed": False,
            }
        )
        sigs[cand.key] = entry
    save_candidates_state(workspace, state)


def dismiss_mined_signatures(workspace: Path) -> int:
    """Mark candidates dismissed when a route records matching mined_from."""
    state = load_candidates_state(workspace)
    sigs = state.setdefault("signatures", {})
    mined = _mined_from_signatures(workspace)
    changed = 0
    for key in mined:
        entry = sigs.get(key)
        if entry is None:
            continue
        if not entry.get("dismissed"):
            entry["dismissed"] = True
            changed += 1
    if changed:
        save_candidates_state(workspace, state)
    return changed


def write_route_candidates_skill(
    workspace: Path,
    candidates: list[RouteCandidate],
    *,
    dry_run: bool = False,
) -> Path | None:
    """Write ``workspace/skills/route-candidates.md`` for the agent skill directory."""
    if not candidates:
        return None
    skills_dir = Path(workspace) / "skills"
    path = skills_dir / "route-candidates.md"
    lines = [
        "---",
        "description: Repeated tool-call sequences that are candidates for agent routes",
        "load: stub",
        "---",
        "",
        "# Route candidates",
        "",
        "These tool-call sequences showed up across multiple successful runs.",
        "Consider authoring a route for any that you still do frequently —",
        "load the `agent-routes` skill for the contract and templates.",
        "",
    ]
    for cand in candidates:
        lines.append(f"## {cand.key}")
        lines.append("")
        lines.append(f"- **Annotated:** `{annotate_signature(cand.signature)}`")
        lines.append(f"- **Runs:** {cand.run_count}")
        if cand.first_seen:
            lines.append(f"- **First seen:** {cand.first_seen}")
        if cand.last_seen:
            lines.append(f"- **Last seen:** {cand.last_seen}")
        if cand.example_args:
            lines.append("- **Example args (truncated):**")
            lines.append("```json")
            lines.append(json.dumps(cand.example_args[0], indent=2, default=str))
            lines.append("```")
        lines.append("")
        lines.append(
            f"Suggested `mined_from` for route.json: "
            f"`{json.dumps(cand.signature)}`"
        )
        lines.append("")

    body = "\n".join(lines)
    if dry_run:
        logger.info("Dry-run: would write %s (%d candidates)", path, len(candidates))
        return path
    skills_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    try:
        from ..skills import invalidate_skill_cache

        invalidate_skill_cache(Path(workspace))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to invalidate skill cache after candidates write: %s", exc)
    return path
