from typing import Protocol, List, Optional
from pydantic import BaseModel

class MemoryResult(BaseModel):
    text: str
    score: float = 0.0

class MemoryBackend(Protocol):
    """Interface all memory backends must implement."""

    def search(self, query: str, agent_id: str,
               user_id: Optional[str] = None, limit: int = 10) -> List[MemoryResult]:
        ...

    def add(self, content: str | list[dict], agent_id: str,
            user_id: Optional[str] = None, run_id: Optional[str] = None,
            metadata: Optional[dict] = None) -> None:
        ...

    def get_all(self, agent_id: str, user_id: Optional[str] = None,
                limit: int = 100) -> List[MemoryResult]:
        ...

def format_memories(memories: List[MemoryResult]) -> str:
    """Format memory results into a string for the system prompt."""
    if not memories:
        return ""
    return "\n".join(f"- {r.text}" for r in memories)

def create_memory_backend(config) -> MemoryBackend:
    if config.provider == "mem0":
        from .mem0 import Mem0Backend
        return Mem0Backend(config)
    raise ValueError(f"Unknown memory provider: {config.provider}")
