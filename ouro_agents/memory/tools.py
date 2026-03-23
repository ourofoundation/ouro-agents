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

    @tool
    def memory_store(fact: str, category: str = "general", importance: float = 0.5) -> str:
        """Store an important fact in long-term memory with optional categorization.
        Args:
            fact: The fact to remember
            category: One of: fact, preference, learning, decision, observation, general
            importance: How important this is (0.0-1.0). 0.3=minor, 0.5=normal, 0.7=significant, 0.9=critical
        """
        valid_categories = set(CATEGORY_LABELS.keys())
        if category not in valid_categories:
            category = "general"
        importance = max(0.0, min(1.0, importance))

        backend.add(
            fact,
            agent_id=agent_id,
            user_id=user_id,
            metadata={
                "category": category,
                "importance": importance,
                "source": "manual",
            },
        )
        return f"Stored [{category}, importance={importance}]: {fact}"

    @tool
    def memory_recall(query: str, category: str = "", limit: int = 5) -> str:
        """Search memory for facts relevant to a query, optionally filtered by category.
        Args:
            query: What to search for
            category: Optional filter — one of: fact, preference, learning, decision, observation, general. Leave empty for all.
            limit: Max results
        """
        results = backend.search(query=query, agent_id=agent_id, user_id=user_id, limit=limit)
        if category:
            results = [r for r in results if r.category == category]
        if not results:
            return "No relevant memories found."

        lines: list[str] = []
        for r in results:
            score_str = f" (score={r.score:.2f})" if r.score > 0 else ""
            cat_str = f" [{r.category}]" if r.category != "general" else ""
            lines.append(f"- {r.text}{cat_str}{score_str}")
        return "\n".join(lines)

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
