from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from smolagents import tool

from . import MemoryBackend
from .model import to_metadata
from .relevance import memory_signal_score
from .session import SessionMemoryStore
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
) -> list:
    allowed_categories = set(memory_categories or [])
    run_team_id = team_id
    run_mode = mode or ""
    session_store = SessionMemoryStore(workspace) if workspace and conversation_id else None
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
        queries: list,
        scope: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        team_id: Optional[str] = None,
        category: Optional[str] = None,
        mode: Optional[str] = None,
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
                - mode/since (str, optional): Origin-mode and ISO timestamp filters.
                - limit (int, optional): Per-query max results.
                - scope (str, optional): Per-query memory scope.
            scope: Default scope for every query: "team" (default, this team's memories) | "personal" (this user's memories in this team) | "org" (all teams) | "global" (everything).
            subject_type: Default subject filter: user, agent, team, asset, conversation, org, or general.
            subject_id: Default subject id filter.
            asset_id: Default asset association filter.
            team_id: Default team association filter.
            category: Default category filter for every query: fact, preference, learning, decision, direction, observation, or general.
            mode: Default origin-mode filter: chat, heartbeat, event, plan, review.
            since: Default ISO timestamp lower bound.
            limit: Default max results per query.

        Example single:  [{"query": "user's favorite language"}]
        Example multi:   [{"query": "API preferences"}, {"query": "past decisions about auth", "category": "decision"}]
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
                mode,
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
                "mode": mode,
                "since": since,
                "limit": limit,
            }
            queries = [
                {
                    **{k: v for k, v in defaults.items() if v is not None and k not in spec},
                    **spec,
                }
                for spec in queries
            ]

        def _search_one(spec: dict) -> tuple[str, list[str]]:
            query = spec.get("query", "")
            category = spec.get("category", "")
            limit = int(spec.get("limit", 5))
            scope = spec.get("scope", "team")
            spec_team_id = spec.get("team_id", run_team_id)
            spec_subject_type = spec.get("subject_type", "")
            spec_subject_id = spec.get("subject_id", "")
            spec_asset_id = spec.get("asset_id", "")
            spec_mode = spec.get("mode", "")
            since_dt = None
            if spec.get("since"):
                try:
                    since_dt = datetime.fromisoformat(str(spec["since"]))
                except ValueError:
                    since_dt = None

            explicit_filters = any(
                spec.get(key)
                for key in ["subject_type", "subject_id", "asset_id", "team_id", "mode", "since"]
            )
            results = []
            if run_mode.startswith("chat") and not explicit_filters:
                if session_store and conversation_id:
                    results.extend(
                        session_store.search(
                            conversation_id,
                            query,
                            limit=limit,
                            category=category or None,
                        )
                    )
                if user_id:
                    results.extend(
                        backend.search(
                            query=query,
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
                            query=query,
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
                            query=query,
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
                    query=query,
                    agent_id=agent_id,
                    user_id=user_id if scope == "personal" or spec_subject_type == "user" else None,
                    limit=limit,
                    team_id=spec_team_id,
                    scope=scope,
                    category=category or None,
                    subject_type=spec_subject_type or None,
                    subject_id=spec_subject_id or None,
                    asset_id=spec_asset_id or None,
                    mode=spec_mode or None,
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
            results = sorted(
                deduped,
                key=lambda r: memory_signal_score(
                    r,
                    team_id=spec_team_id or "",
                    explicit_filter=explicit_filters,
                ),
                reverse=True,
            )

            lines: list[str] = []
            for r in results:
                score_str = f" (score={r.score:.2f})" if r.score > 0 else ""
                cat_str = f" [{r.category}]" if r.category != "general" else ""
                refs = getattr(r, "asset_ids", []) or []
                if not refs and hasattr(r, "metadata"):
                    raw_refs = getattr(r, "metadata", {}).get("asset_ids") or getattr(r, "metadata", {}).get("asset_refs", "")
                    refs = [x for x in raw_refs.split(",") if x] if isinstance(raw_refs, str) else raw_refs
                ref_str = f" refs={','.join(refs)}" if refs else ""
                lines.append(f"- {r.text}{cat_str}{score_str}{ref_str}")
                if len(lines) >= limit:
                    break
            return query, lines

        if len(queries) == 1:
            query, lines = _search_one(queries[0])
            return "\n".join(lines) if lines else "No relevant memories found."

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
                all_sections.append(f"## Query: \"{query}\"\n" + "\n".join(lines))
            else:
                all_sections.append(f"## Query: \"{query}\"\nNo relevant memories found.")

        return "\n\n".join(all_sections)

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

        today = date.today().isoformat()

        if doc_store:
            memory_name = doc_store.memory_name(agent_id)
            daily_name = doc_store.daily_name(agent_id, today)
            content = doc_store.read(memory_name)
            if content:
                tokens = len(content) // 4
                lines.append(f"Working memory: ~{tokens} tokens")

            daily_content = doc_store.read(daily_name)
            if daily_content:
                entry_count = sum(
                    1 for line in daily_content.split("\n") if line.strip().startswith("-")
                )
                lines.append(f"Today's log: {entry_count} entries")

            from .ouro_docs import OuroDocStore
            storage = "Ouro posts (shared)" if isinstance(doc_store, OuroDocStore) else "local files"
            lines.append(f"Storage: {storage}")

        if workspace:
            entities_dir = workspace / "memory" / "entities"
            if entities_dir.exists():
                entity_files = list(entities_dir.glob("*.md"))
                if entity_files:
                    lines.append(f"Entity files: {len(entity_files)}")

        return "\n".join(lines)

    @tool
    def remember(
        text: str,
        subject_type: str,
        category: str,
        subject_id: str = "",
        asset_ids: Optional[list[str]] = None,
        team_slug_or_id: str = "",
        importance: float = 0.5,
        reason: str = "",
    ) -> str:
        """Store a durable memory through validation. Disabled unless the run opts in.

        Args:
            text: Memory text to store.
            subject_type: user, agent, team, asset, conversation, org, or general.
            category: direction, decision, fact, preference, learning, or observation.
            subject_id: Optional subject identifier. Use "self" for the current agent.
            asset_ids: Optional Ouro asset UUIDs referenced by this memory.
            team_slug_or_id: Optional available team slug or ID to associate.
            importance: 0.0-1.0 memory importance.
            reason: Required explanation for why this should be durable.
        """
        if not enable_remember:
            return json.dumps({"status": "error", "error": "remember is not enabled for this run"})
        if not reason.strip():
            return json.dumps({"status": "error", "error": "reason is required"})

        resolved_team_ids: list[str] = []
        if team_slug_or_id:
            resolved = team_lookup.get(team_slug_or_id)
            if not resolved:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"unknown or unavailable team: {team_slug_or_id}",
                    }
                )
            resolved_team_ids = [resolved]

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
        try:
            item = validate_memory_candidate(
                {
                    "text": text,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "category": category,
                    "asset_ids": asset_ids or [],
                    "team_ids": resolved_team_ids,
                    "importance": importance,
                    "confidence": 1.0,
                },
                ctx,
                source="remember-tool",
            )
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        backend.add(
            item.text,
            agent_id=agent_id,
            user_id=user_id,
            run_id=run_id or conversation_id,
            metadata=to_metadata(item),
            team_id=item.team_ids[0] if item.team_ids else None,
        )
        return json.dumps({"status": "ok", "content_hash": item.content_hash})

    tools = [memory_recall, memory_status]
    if enable_remember:
        tools.append(remember)
    return tools
