from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from smolagents import tool

from . import MemoryBackend
from .model import to_metadata
from .naming import period_key, store_rhythm
from .relevance import memory_signal_score
from .validator import MemoryRunContext, validate_memory_candidate

if TYPE_CHECKING:
    from . import DocStore


def _normalize_memory_queries(raw_queries: Any) -> list[dict]:
    """Accept common near-miss shapes without letting malformed specs crash."""

    if isinstance(raw_queries, dict):
        nested = raw_queries.get("queries")
        if isinstance(nested, list):
            raw_queries = nested
        elif isinstance(nested, str):
            raw_queries = [nested]
        elif isinstance(raw_queries.get("query"), str):
            raw_queries = [raw_queries]
        else:
            return []

    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]

    if not isinstance(raw_queries, list):
        return []

    queries: list[dict] = []
    for spec in raw_queries:
        if isinstance(spec, str):
            text = spec.strip()
            if text:
                queries.append({"query": text})
            continue

        if not isinstance(spec, dict):
            continue

        nested = spec.get("queries")
        if isinstance(nested, list):
            queries.extend(_normalize_memory_queries(nested))
            continue

        query = spec.get("query", "")
        if isinstance(query, str) and query.strip():
            queries.append(spec)

    return queries


def _normalize_remember_specs(raw: Any) -> list[dict]:
    """Accept list[dict], a single dict, or ``{"memories": [...]}``."""

    if isinstance(raw, dict):
        nested = raw.get("memories")
        if isinstance(nested, list):
            raw = nested
        elif isinstance(raw.get("text"), str):
            raw = [raw]
        else:
            return []

    if not isinstance(raw, list):
        return []

    specs: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            specs.append(item)
    return specs


def _normalize_forget_specs(raw: Any) -> list[dict]:
    """Accept list of ``{memory_id, reason}``, a single dict, or shared-reason form.

    Shared-reason form: ``{"memory_ids": ["a", "b"], "reason": "..."}``.
    """

    if isinstance(raw, dict):
        nested = raw.get("items")
        if isinstance(nested, list):
            raw = nested
        elif isinstance(raw.get("memory_ids"), list) and isinstance(
            raw.get("reason"), str
        ):
            reason = raw["reason"]
            raw = [
                {"memory_id": str(mid), "reason": reason}
                for mid in raw["memory_ids"]
                if str(mid).strip()
            ]
        elif raw.get("memory_id") is not None:
            raw = [raw]
        else:
            return []

    if not isinstance(raw, list):
        return []

    specs: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                specs.append({"memory_id": text, "reason": ""})
            continue
        if isinstance(item, dict) and item.get("memory_id") is not None:
            specs.append(item)
    return specs


def make_memory_tools(
    backend: MemoryBackend,
    agent_id: str,
    user_id: Optional[str] = None,
    workspace: Optional[Path] = None,
    doc_store: Optional["DocStore"] = None,
    team_id: Optional[str] = None,
    memory_categories: Optional[list[str]] = None,
    conversation_id: Optional[str] = None,
    run_id: str = "",
    mode: str = "",
    event_type: str = "",
    org_id: str = "",
    available_team_ids: Optional[set[str]] = None,
    available_teams: Optional[list[dict]] = None,
    enable_remember: bool = False,
    search_limit: int = 5,
    max_retrieval_tokens: int = 4000,
    min_signal_score: float = 0.35,
) -> list:
    allowed_categories = set(memory_categories or [])
    # Global character budget across all recall output for a single call.
    retrieval_char_budget = max(int(max_retrieval_tokens), 0) * 4
    run_team_id = team_id
    run_mode = mode or ""
    team_ids = set(available_team_ids or ([] if not run_team_id else [run_team_id]))
    team_lookup: dict[str, str] = {}
    for team in available_teams or []:
        tid = str(team.get("id") or "")
        if not tid:
            continue
        team_lookup[tid] = tid
        if team.get("slug"):
            team_lookup[str(team["slug"])] = tid

    @tool
    def memory_recall(
        queries: list[dict],
        scope: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        team_id: Optional[str] = None,
        category: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> str:
        """Search memory for facts relevant to one or more queries. Results are grouped by query.

        Args:
            queries: List of search specs. Each is either a string or a dict with keys:
                - query (str, required): What to search for
                - category (str, optional): Per-query category filter. Omit for all.
                - subject_type/subject_id (str, optional): What the memory is about.
                - team_id/asset_id (str, optional): Association filters.
                - since (str, optional): ISO timestamp lower bound.
                - limit (int, optional): Per-query max results.
                - scope (str, optional): Per-query memory scope.
            scope: Default scope for every query: "team" (default, this team's memories) | "personal" (this user's memories in this team) | "org" (all teams) | "global" (everything).
            subject_type: Default subject filter: user, agent, team, asset, conversation, org, or general.
            subject_id: Default subject id filter.
            asset_id: Default asset association filter.
            team_id: Default team association filter.
            category: Default category filter for every query: fact, preference, direction.
            since: Default ISO timestamp lower bound.
            limit: Default max results per query.

        Example single:  [{"query": "user's favorite language"}]
        Example multi:   [{"query": "API preferences"}, {"query": "past decisions about auth", "category": "direction"}]
        """
        queries = _normalize_memory_queries(queries)
        if not queries:
            return "No queries provided."
        if any(
            value is not None
            for value in [
                scope,
                subject_type,
                subject_id,
                asset_id,
                team_id,
                category,
                since,
                limit,
            ]
        ):
            defaults = {
                "scope": scope,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "asset_id": asset_id,
                "team_id": team_id,
                "category": category,
                "since": since,
                "limit": limit,
            }
            queries = [
                {
                    **{
                        k: v
                        for k, v in defaults.items()
                        if v is not None and k not in spec
                    },
                    **spec,
                }
                for spec in queries
            ]

        def _search_one(spec: dict) -> tuple[str, list[str]]:
            query = spec.get("query", "")
            category = spec.get("category", "")
            limit = int(spec.get("limit", search_limit))
            scope = spec.get("scope", "team")
            spec_team_id = spec.get("team_id", run_team_id)
            spec_subject_type = spec.get("subject_type", "")
            spec_subject_id = spec.get("subject_id", "")
            spec_asset_id = spec.get("asset_id", "")
            since_dt = None
            if spec.get("since"):
                try:
                    since_dt = datetime.fromisoformat(str(spec["since"]))
                except ValueError:
                    since_dt = None

            explicit_filters = any(
                spec.get(key)
                for key in [
                    "subject_type",
                    "subject_id",
                    "asset_id",
                    "team_id",
                    "since",
                ]
            )
            search_query = query
            results = []
            if run_mode.startswith("chat") and not explicit_filters:
                if user_id:
                    results.extend(
                        backend.search(
                            query=search_query,
                            agent_id=agent_id,
                            user_id=user_id,
                            limit=limit,
                            scope="global",
                            category=category or None,
                            subject_type="user",
                            subject_id=user_id,
                            since=since_dt,
                        )
                    )
                for default_subject in ["agent", "general"]:
                    results.extend(
                        backend.search(
                            query=search_query,
                            agent_id=agent_id,
                            limit=limit,
                            team_id=spec_team_id if run_team_id else None,
                            scope="team" if run_team_id else "global",
                            category=category or None,
                            subject_type=default_subject,
                            since=since_dt,
                        )
                    )
                if run_team_id:
                    results.extend(
                        backend.search(
                            query=search_query,
                            agent_id=agent_id,
                            limit=limit,
                            team_id=run_team_id,
                            scope="team",
                            category=category or None,
                            since=since_dt,
                        )
                    )
            else:
                results = backend.search(
                    query=search_query,
                    agent_id=agent_id,
                    user_id=(
                        user_id
                        if scope == "personal" or spec_subject_type == "user"
                        else None
                    ),
                    limit=limit,
                    team_id=spec_team_id,
                    scope=scope,
                    category=category or None,
                    subject_type=spec_subject_type or None,
                    subject_id=spec_subject_id or None,
                    asset_id=spec_asset_id or None,
                    since=since_dt,
                )

            if category:
                results = [r for r in results if r.category == category]
            if allowed_categories:
                results = [r for r in results if r.category in allowed_categories]

            deduped = []
            seen_text: set[str] = set()
            for r in results:
                key = r.text.strip().lower()
                if not key or key in seen_text:
                    continue
                seen_text.add(key)
                deduped.append(r)
            scored = [
                (
                    memory_signal_score(
                        r,
                        team_id=spec_team_id or "",
                        explicit_filter=explicit_filters,
                    ),
                    r,
                )
                for r in deduped
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            # Relevance floor: drop low-signal hits so recall stays high-precision.
            # Skipped when the caller passed explicit filters (they asked for
            # exactly this slice) — but never return an empty set if anything
            # matched at all; keep the single best hit as a fallback.
            if not explicit_filters and scored:
                filtered = [pair for pair in scored if pair[0] >= min_signal_score]
                scored = filtered if filtered else scored[:1]
            results = [r for _, r in scored]

            lines: list[str] = []
            for r in results:
                score_str = f" (score={r.score:.2f})" if r.score > 0 else ""
                cat_str = f" [{r.category}]" if r.category != "general" else ""
                refs = getattr(r, "asset_ids", []) or []
                if not refs and hasattr(r, "metadata"):
                    raw_refs = getattr(r, "metadata", {}).get("asset_ids") or getattr(
                        r, "metadata", {}
                    ).get("asset_refs", "")
                    refs = (
                        [x for x in raw_refs.split(",") if x]
                        if isinstance(raw_refs, str)
                        else raw_refs
                    )
                ref_str = f" refs={','.join(refs)}" if refs else ""
                id_str = f" (id={r.id})" if enable_remember and r.id else ""
                lines.append(f"- {r.text}{cat_str}{score_str}{ref_str}{id_str}")
                if len(lines) >= limit:
                    break
            return query, lines

        def _apply_char_budget(sections: list[str]) -> str:
            """Enforce the global retrieval token budget across all sections.

            Whole memory lines are dropped (never mid-line truncated) once the
            budget is exhausted, with a note so the model knows recall was capped.
            """
            text = "\n\n".join(sections)
            if not retrieval_char_budget or len(text) <= retrieval_char_budget:
                return text
            kept: list[str] = []
            used = 0
            truncated = False
            for section in sections:
                section_lines = section.split("\n")
                kept_lines: list[str] = []
                for line in section_lines:
                    cost = len(line) + 1
                    if used + cost > retrieval_char_budget and kept_lines:
                        truncated = True
                        break
                    used += cost
                    kept_lines.append(line)
                if kept_lines:
                    kept.append("\n".join(kept_lines))
                if truncated:
                    break
            result = "\n\n".join(kept)
            if truncated or len(kept) < len(sections):
                result += (
                    "\n\n[Recall output truncated to fit the retrieval budget; "
                    "narrow your queries or add filters for more specific results.]"
                )
            return result

        if len(queries) == 1:
            query, lines = _search_one(queries[0])
            if not lines:
                return "No relevant memories found."
            return _apply_char_budget(["\n".join(lines)])

        all_sections: list[str] = []
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            future_to_idx = {
                pool.submit(_search_one, spec): i for i, spec in enumerate(queries)
            }
            ordered: list[tuple[str, list[str]]] = [("", []) for _ in queries]
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                ordered[idx] = future.result()

        for query, lines in ordered:
            if lines:
                all_sections.append(f'## Query: "{query}"\n' + "\n".join(lines))
            else:
                all_sections.append(f'## Query: "{query}"\nNo relevant memories found.')

        return _apply_char_budget(all_sections)

    @tool
    def memory_status() -> str:
        """Show memory system status: total memories, working memory size, recent daily log activity."""
        lines: list[str] = ["## Memory Status"]

        try:
            all_mems = backend.get_all(
                agent_id=agent_id,
                user_id=user_id,
                limit=200,
                team_id=team_id,
            )
            lines.append(f"Total memories in vector store: {len(all_mems)}")

            cat_counts: dict[str, int] = {}
            subject_counts: dict[str, int] = {}
            team_assoc = 0
            asset_assoc = 0
            schema_counts: dict[str, int] = {}
            for m in all_mems:
                cat_counts[m.category] = cat_counts.get(m.category, 0) + 1
                subject = getattr(m, "subject_type", "") or "legacy"
                subject_counts[subject] = subject_counts.get(subject, 0) + 1
                if getattr(m, "team_ids", None) or m.team_id:
                    team_assoc += 1
                if getattr(m, "asset_ids", None):
                    asset_assoc += 1
                schema = str(getattr(m, "schema_version", 1) or 1)
                schema_counts[schema] = schema_counts.get(schema, 0) + 1
            if cat_counts:
                cat_str = ", ".join(f"{k}: {v}" for k, v in sorted(cat_counts.items()))
                lines.append(f"By category: {cat_str}")
            if subject_counts:
                subject_str = ", ".join(
                    f"{k}: {v}" for k, v in sorted(subject_counts.items())
                )
                lines.append(f"By subject: {subject_str}")
            lines.append(f"With team associations: {team_assoc}")
            lines.append(f"With asset associations: {asset_assoc}")
            if schema_counts:
                schema_str = ", ".join(
                    f"v{k}: {v}" for k, v in sorted(schema_counts.items())
                )
                lines.append(f"By schema: {schema_str}")
        except Exception:
            lines.append("Vector store: unable to query")

        if doc_store:
            rhythm = store_rhythm(doc_store)
            period = period_key(rhythm)
            memory_name = doc_store.memory_name(agent_id)
            log_name = doc_store.log_name(agent_id, period)
            content = doc_store.read(memory_name)
            if content:
                tokens = len(content) // 4
                lines.append(f"Working memory: ~{tokens} tokens")

            log_content = doc_store.read(log_name)
            if log_content:
                entry_count = sum(
                    1
                    for line in log_content.split("\n")
                    if line.strip().startswith("-")
                )
                lines.append(f"Current log: {entry_count} entries")

            from .ouro_docs import OuroDocStore

            storage = (
                "Ouro posts (shared)"
                if isinstance(doc_store, OuroDocStore)
                else "local files"
            )
            lines.append(f"Storage: {storage}")

        if workspace:
            from ..tools.workspace_paths import protected_memory

            entities_dir = protected_memory(workspace) / "entities"
            if entities_dir.exists():
                entity_files = list(entities_dir.glob("*.md"))
                if entity_files:
                    lines.append(f"Entity files: {len(entity_files)}")

        return "\n".join(lines)

    @tool
    def remember(memories: list[dict]) -> str:
        """Store one or more durable memories through validation.

        Disabled unless the run opts in. Batch related facts into one call.

        Args:
            memories: List of memory specs. Each dict supports:
                - text (str, required): Memory text to store.
                - subject_type (str, required): user, agent, team, asset,
                  conversation, org, or general.
                - category (str, required): direction, fact, or preference.
                - reason (str, required): Why this should be durable.
                - subject_id (str, optional): Use "self" for the current agent.
                - asset_ids (list[str], optional): Related Ouro asset UUIDs.
                - team_slug_or_id (str, optional): Available team slug or ID.
                - basis (str, optional): stated, observed, or inferred.
                - stability (str, optional): stable or evolving.
                - strength (float, optional): 0.3 minor, 0.5 normal, 0.8 high.
                - verification_hint (str, optional): How to re-check evolving memories.

        Example single: [{"text": "...", "subject_type": "user", "category": "preference", "reason": "..."}]
        Example multi: [{"text": "...", "subject_type": "agent", "category": "fact", "reason": "..."}, ...]
        """
        if not enable_remember:
            return json.dumps(
                {"status": "error", "error": "remember is not enabled for this run"}
            )

        specs = _normalize_remember_specs(memories)
        if not specs:
            return json.dumps(
                {"status": "error", "error": "No memories provided."}
            )

        ctx = MemoryRunContext(
            agent_id=agent_id,
            user_id=user_id or "",
            org_id=org_id,
            conversation_id=conversation_id or "",
            run_id=run_id,
            mode=run_mode,
            event_type=event_type,
            team_id=run_team_id or "",
            available_team_ids=team_ids,
        )

        results: list[dict] = []
        for index, spec in enumerate(specs):
            text = str(spec.get("text") or "").strip()
            reason = str(spec.get("reason") or "").strip()
            if not text:
                results.append(
                    {"status": "error", "index": index, "error": "text is required"}
                )
                continue
            if not reason:
                results.append(
                    {"status": "error", "index": index, "error": "reason is required"}
                )
                continue

            team_slug_or_id = str(spec.get("team_slug_or_id") or "").strip()
            resolved_team_ids: list[str] = []
            if team_slug_or_id:
                resolved = team_lookup.get(team_slug_or_id)
                if not resolved:
                    results.append(
                        {
                            "status": "error",
                            "index": index,
                            "error": f"unknown or unavailable team: {team_slug_or_id}",
                        }
                    )
                    continue
                resolved_team_ids = [resolved]

            try:
                item = validate_memory_candidate(
                    {
                        "text": text,
                        "subject_type": spec.get("subject_type", ""),
                        "subject_id": spec.get("subject_id", ""),
                        "category": spec.get("category", ""),
                        "asset_ids": spec.get("asset_ids") or [],
                        "team_ids": resolved_team_ids,
                        "basis": spec.get("basis", "stated"),
                        "stability": spec.get("stability", "stable"),
                        "strength": spec.get("strength", 0.5),
                        "verification_hint": spec.get("verification_hint", ""),
                    },
                    ctx,
                    source="remember-tool",
                )
            except ValueError as exc:
                results.append(
                    {"status": "error", "index": index, "error": str(exc)}
                )
                continue

            backend.add(
                item.text,
                agent_id=agent_id,
                user_id=user_id,
                run_id=run_id or conversation_id,
                metadata=to_metadata(item),
                team_id=item.team_ids[0] if item.team_ids else None,
                infer=False,
            )
            results.append(
                {
                    "status": "ok",
                    "index": index,
                    "content_hash": item.content_hash,
                }
            )

        ok_count = sum(1 for r in results if r.get("status") == "ok")
        if ok_count == len(results):
            status = "ok"
        elif ok_count == 0:
            status = "error"
        else:
            status = "partial"
        return json.dumps({"status": status, "results": results})

    @tool
    def update_memory(memory_id: str, text: str, reason: str) -> str:
        """Revise a memory in place when it is partly wrong or has evolved.

        Replaces the stored text while keeping the memory's scope and category,
        so a single contradicting fact supersedes the stale one immediately
        instead of waiting for background maintenance. Get ids from
        memory_recall. Disabled unless the run opts in.

        Args:
            memory_id: Id of the memory to revise (from memory_recall output).
            text: Corrected memory text that replaces the existing text.
            reason: Why this memory is being revised.
        """
        if not enable_remember:
            return json.dumps(
                {"status": "error", "error": "update_memory is not enabled for this run"}
            )
        if not memory_id.strip():
            return json.dumps({"status": "error", "error": "memory_id is required"})
        if not text.strip():
            return json.dumps({"status": "error", "error": "text is required"})
        if not reason.strip():
            return json.dumps({"status": "error", "error": "reason is required"})
        try:
            backend.update_text(memory_id, text)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})
        return json.dumps({"status": "ok", "updated": memory_id})

    @tool
    def forget(items: list[dict]) -> str:
        """Permanently delete one or more memories by id.

        Use when memories are wrong, outdated, or fully superseded and should no
        longer surface. For a memory that merely changed, prefer update_memory.
        Get ids from memory_recall. Disabled unless the run opts in. Batch
        related deletes into one call.

        Args:
            items: List of delete specs. Each dict supports:
                - memory_id (str, required): Id from memory_recall.
                - reason (str, required): Why this memory should be removed.
            Shared-reason form also accepted: {"memory_ids": ["a", "b"], "reason": "..."}.

        Example single: [{"memory_id": "mem-1", "reason": "superseded"}]
        Example multi: [{"memory_id": "a", "reason": "..."}, {"memory_id": "b", "reason": "..."}]
        """
        if not enable_remember:
            return json.dumps(
                {"status": "error", "error": "forget is not enabled for this run"}
            )

        specs = _normalize_forget_specs(items)
        if not specs:
            return json.dumps({"status": "error", "error": "No items provided."})

        results: list[dict] = []
        for index, spec in enumerate(specs):
            memory_id = str(spec.get("memory_id") or "").strip()
            reason = str(spec.get("reason") or "").strip()
            if not memory_id:
                results.append(
                    {
                        "status": "error",
                        "index": index,
                        "error": "memory_id is required",
                    }
                )
                continue
            if not reason:
                results.append(
                    {
                        "status": "error",
                        "index": index,
                        "memory_id": memory_id,
                        "error": "reason is required",
                    }
                )
                continue
            try:
                backend.delete(memory_id)
            except Exception as exc:
                results.append(
                    {
                        "status": "error",
                        "index": index,
                        "memory_id": memory_id,
                        "error": str(exc),
                    }
                )
                continue
            results.append(
                {"status": "ok", "index": index, "deleted": memory_id}
            )

        ok_count = sum(1 for r in results if r.get("status") == "ok")
        if ok_count == len(results):
            status = "ok"
        elif ok_count == 0:
            status = "error"
        else:
            status = "partial"
        return json.dumps({"status": status, "results": results})

    tools = [memory_recall, memory_status]
    if enable_remember:
        tools.extend([remember, update_memory, forget])
    return tools
