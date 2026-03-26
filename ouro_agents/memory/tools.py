from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from smolagents import tool

from . import MemoryBackend

if TYPE_CHECKING:
    from .ouro_docs import OuroDocStore


def make_memory_tools(
    backend: MemoryBackend,
    agent_id: str,
    user_id: Optional[str] = None,
    workspace: Optional[Path] = None,
    doc_store: Optional[OuroDocStore] = None,
) -> list:

    @tool
    def memory_recall(queries: list) -> str:
        """Search memory for facts relevant to one or more queries. Results are grouped by query.

        Args:
            queries: List of search specs. Each is a dict with keys:
                - query (str, required): What to search for
                - category (str, optional): Filter — one of: fact, preference, learning, decision, observation, general. Omit for all.
                - limit (int, optional): Max results per query (default: 5)

        Example single:  [{"query": "user's favorite language"}]
        Example multi:   [{"query": "API preferences"}, {"query": "past decisions about auth", "category": "decision"}]
        """
        if not queries:
            return "No queries provided."

        def _search_one(spec: dict) -> tuple[str, list[str]]:
            if isinstance(spec, str):
                spec = {"query": spec}
            query = spec.get("query", "")
            category = spec.get("category", "")
            limit = int(spec.get("limit", 5))

            results = backend.search(
                query=query, agent_id=agent_id, user_id=user_id, limit=limit,
            )
            if category:
                results = [r for r in results if r.category == category]

            lines: list[str] = []
            for r in results:
                score_str = f" (score={r.score:.2f})" if r.score > 0 else ""
                cat_str = f" [{r.category}]" if r.category != "general" else ""
                raw_refs = getattr(r, "metadata", {}).get("asset_refs", "") if hasattr(r, "metadata") else ""
                refs = [x for x in raw_refs.split(",") if x] if isinstance(raw_refs, str) else raw_refs
                ref_str = f" refs={','.join(refs)}" if refs else ""
                lines.append(f"- {r.text}{cat_str}{score_str}{ref_str}")
            return query, lines

        if len(queries) == 1:
            query, lines = _search_one(queries[0])
            return "\n".join(lines) if lines else "No relevant memories found."

        all_sections: list[str] = []
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            future_to_idx = {
                pool.submit(_search_one, spec): i for i, spec in enumerate(queries)
            }
            ordered: list[tuple[str, list[str]]] = [("", [])] * len(queries)
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
            all_mems = backend.get_all(agent_id=agent_id, user_id=user_id, limit=200)
            lines.append(f"Total memories in vector store: {len(all_mems)}")

            cat_counts: dict[str, int] = {}
            for m in all_mems:
                cat_counts[m.category] = cat_counts.get(m.category, 0) + 1
            if cat_counts:
                cat_str = ", ".join(f"{k}: {v}" for k, v in sorted(cat_counts.items()))
                lines.append(f"By category: {cat_str}")
        except Exception:
            lines.append("Vector store: unable to query")

        today = date.today().isoformat()

        if doc_store:
            content = doc_store.read(f"MEMORY:{agent_id}")
            if content:
                tokens = len(content) // 4
                lines.append(f"Working memory (MEMORY post): ~{tokens} tokens")

            daily_content = doc_store.read(f"DAILY:{agent_id}:{today}")
            if daily_content:
                entry_count = sum(
                    1 for line in daily_content.split("\n") if line.strip().startswith("-")
                )
                lines.append(f"Today's log: {entry_count} entries")

            lines.append("Storage: Ouro posts (shared)")
        elif workspace:
            memory_md = workspace / "MEMORY.md"
            if memory_md.exists():
                content = memory_md.read_text()
                tokens = len(content) // 4
                lines.append(f"Working memory (MEMORY.md): ~{tokens} tokens")

            today_log = workspace / "memory" / "daily" / f"{today}.md"
            if today_log.exists():
                log_lines = today_log.read_text().strip().split("\n")
                entry_count = sum(1 for line in log_lines if line.strip().startswith("-"))
                lines.append(f"Today's log: {entry_count} entries")

            lines.append("Storage: local files")

        if workspace:
            entities_dir = workspace / "memory" / "entities"
            if entities_dir.exists():
                entity_files = list(entities_dir.glob("*.md"))
                if entity_files:
                    lines.append(f"Entity files: {len(entity_files)}")

        return "\n".join(lines)

    return [memory_recall, memory_status]
