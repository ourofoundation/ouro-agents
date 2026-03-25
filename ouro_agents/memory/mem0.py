import logging
from datetime import datetime, timezone
from typing import List, Optional

from ..config import MemoryConfig
from . import MemoryBackend, MemoryResult

logger = logging.getLogger(__name__)


def _split_provider_and_model(model_id: str) -> tuple[Optional[str], str]:
    """Normalize 'openai/gpt-4o-mini' into ('openai', 'gpt-4o-mini')."""
    if "/" in model_id:
        provider, model = model_id.split("/", 1)
        if provider in {"openai", "anthropic"} and model:
            return provider, model
    return None, model_id


def _extract_metadata(raw: dict) -> dict:
    """Pull our custom fields out of a mem0 result's metadata."""
    meta = raw.get("metadata", {}) or {}
    return {
        "category": meta.get("category", "general"),
        "importance": meta.get("importance", 0.5),
        "created_at": meta.get("created_at", ""),
        "source": meta.get("source", ""),
        "last_accessed": meta.get("last_accessed", ""),
    }


class Mem0Backend:
    def __init__(self, config: MemoryConfig):
        from mem0 import Memory

        chroma_path = config.path / "chroma"
        chroma_path.mkdir(parents=True, exist_ok=True)

        llm_provider, llm_model = _split_provider_and_model(config.extraction_model)
        if llm_provider is None:
            llm_provider = (
                "anthropic" if "claude" in config.extraction_model else "openai"
            )

        embedder_provider, embedder_model = _split_provider_and_model(config.embedder)
        if embedder_provider is None:
            embedder_provider = (
                "openai" if "text-embedding" in config.embedder else "huggingface"
            )

        mem0_config = {
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "ouro_agent_memory",
                    "path": str(chroma_path),
                },
            },
            "llm": {"provider": llm_provider, "config": {"model": llm_model}},
            "embedder": {
                "provider": embedder_provider,
                "config": {"model": embedder_model},
            },
        }

        if config.graph and config.graph.enabled:
            mem0_config["graph_store"] = {
                "provider": config.graph.provider,
                "config": config.graph.config,
            }

        self._mem = Memory.from_config(mem0_config)

    def search(
        self,
        query: str,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[MemoryResult]:
        kwargs: dict = {"query": query, "agent_id": agent_id, "limit": limit}
        if user_id:
            kwargs["user_id"] = user_id
        results = self._mem.search(**kwargs)
        res_list = results.get("results", []) if isinstance(results, dict) else results

        out: list[MemoryResult] = []
        for r in res_list:
            meta = _extract_metadata(r)
            out.append(
                MemoryResult(
                    text=r["memory"],
                    score=r.get("score", 0),
                    **meta,
                )
            )
        return out

    def add(
        self,
        content: str | list[dict],
        agent_id: str,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        meta = dict(metadata or {})
        meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        meta.setdefault("category", "general")
        meta.setdefault("importance", 0.5)

        kwargs: dict = {"agent_id": agent_id, "metadata": meta}
        if user_id:
            kwargs["user_id"] = user_id
        if run_id:
            kwargs["run_id"] = run_id
        self._mem.add(content, **kwargs)

    def get_all(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryResult]:
        kwargs: dict = {"agent_id": agent_id, "limit": limit}
        if user_id:
            kwargs["user_id"] = user_id
        results = self._mem.get_all(**kwargs)
        res_list = results.get("results", []) if isinstance(results, dict) else results
        out: list[MemoryResult] = []
        for r in res_list:
            meta = _extract_metadata(r)
            out.append(MemoryResult(text=r["memory"], score=0, **meta))
        return out

    def update_metadata(self, memory_id: str, metadata: dict) -> None:
        try:
            self._mem.update(memory_id, metadata=metadata)
        except Exception as e:
            logger.warning("Failed to update memory metadata %s: %s", memory_id, e)
