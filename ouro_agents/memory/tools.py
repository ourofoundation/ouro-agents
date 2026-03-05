from smolagents import tool
from . import MemoryBackend

def make_memory_tools(backend: MemoryBackend, agent_id: str) -> list:

    @tool
    def memory_store(fact: str) -> str:
        """Store an important fact in long-term memory.
        Args:
            fact: The fact to remember
        """
        backend.add(fact, agent_id=agent_id)
        return f"Stored: {fact}"

    @tool
    def memory_recall(query: str, limit: int = 5) -> str:
        """Search memory for facts relevant to a query.
        Args:
            query: What to search for
            limit: Max results
        """
        results = backend.search(query=query, agent_id=agent_id, limit=limit)
        if not results:
            return "No relevant memories found."
        return "\n".join(f"- {r.text}" for r in results)

    return [memory_store, memory_recall]
