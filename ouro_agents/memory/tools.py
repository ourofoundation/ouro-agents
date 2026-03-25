from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional

from smolagents import tool

from . import CATEGORY_LABELS, MemoryBackend


def make_memory_tools(
    backend: MemoryBackend,
    agent_id: str,
    user_id: Optional[str] = None,
    workspace: Optional[Path] = None,
) -> list:

    valid_categories = set(CATEGORY_LABELS.keys())

    @tool
    def memory_store(facts: list) -> str:
        """Store one or more facts in long-term memory.

        Args:
            facts: List of facts to store. Each is a dict with keys:
                - fact (str, required): The fact to remember
                - category (str, optional): One of: fact, preference, learning, decision, observation, general (default: general)
                - importance (float, optional): 0.0–1.0. 0.3=minor, 0.5=normal, 0.7=significant, 0.9=critical (default: 0.5)

        Example single:  [{"fact": "User prefers dark mode", "category": "preference"}]
        Example multi:   [{"fact": "Uses PostgreSQL", "category": "fact"}, {"fact": "Prefers concise answers", "category": "preference", "importance": 0.7}]
        """
        if not facts:
            return "No facts provided."

        results: list[str] = []
        for entry in facts:
            if isinstance(entry, str):
                entry = {"fact": entry}
            fact = entry.get("fact", "")
            if not fact:
                continue
            cat = entry.get("category", "general")
            if cat not in valid_categories:
                cat = "general"
            imp = max(0.0, min(1.0, float(entry.get("importance", 0.5))))

            backend.add(
                fact,
                agent_id=agent_id,
                user_id=user_id,
                metadata={
                    "category": cat,
                    "importance": imp,
                    "source": "manual",
                },
            )
            results.append(f"Stored [{cat}, importance={imp}]: {fact}")

        return "\n".join(results) if results else "No valid facts provided."

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
                lines.append(f"- {r.text}{cat_str}{score_str}")
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
        """Show memory system status: total memories, MEMORY.md size, recent daily log activity."""
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

        if workspace:
            memory_md = workspace / "MEMORY.md"
            if memory_md.exists():
                content = memory_md.read_text()
                tokens = len(content) // 4
                lines.append(f"MEMORY.md: ~{tokens} tokens")
            else:
                lines.append("MEMORY.md: not found")

            today_log = workspace / "memory" / "daily" / f"{date.today().isoformat()}.md"
            if today_log.exists():
                log_lines = today_log.read_text().strip().split("\n")
                entry_count = sum(1 for l in log_lines if l.strip().startswith("-"))
                lines.append(f"Today's log: {entry_count} entries")
            else:
                lines.append("Today's log: empty")

            entities_dir = workspace / "memory" / "entities"
            if entities_dir.exists():
                entity_files = list(entities_dir.glob("*.md"))
                lines.append(f"Entity files: {len(entity_files)}")

            tasks_dir = workspace / "memory" / "tasks"
            if tasks_dir.exists():
                task_files = list(tasks_dir.glob("*.md"))
                lines.append(f"Task files: {len(task_files)}")

        return "\n".join(lines)

    return [memory_store, memory_recall, memory_status]
