from typing import List, Optional
from pathlib import Path
from . import MemoryBackend, MemoryResult
from ..config import MemoryConfig


def _split_provider_and_model(model_id: str) -> tuple[Optional[str], str]:
    """
    Normalize provider-prefixed model ids like 'openai/gpt-4o-mini'
    into ('openai', 'gpt-4o-mini').
    """
    if "/" in model_id:
        provider, model = model_id.split("/", 1)
        if provider in {"openai", "anthropic"} and model:
            return provider, model
    return None, model_id


class Mem0Backend:
    def __init__(self, config: MemoryConfig):
        from mem0 import Memory

        # Ensure path exists
        chroma_path = config.path / "chroma"
        chroma_path.mkdir(parents=True, exist_ok=True)

        llm_provider, llm_model = _split_provider_and_model(config.extraction_model)
        if llm_provider is None:
            llm_provider = "anthropic" if "claude" in config.extraction_model else "openai"

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
                }
            },
            "llm": {
                "provider": llm_provider,
                "config": {"model": llm_model}
            },
            "embedder": {
                "provider": embedder_provider,
                "config": {"model": embedder_model}
            }
        }

        # Graph is opt-in
        if config.graph and config.graph.enabled:
            mem0_config["graph_store"] = {
                "provider": config.graph.provider,
                "config": config.graph.config,
            }

        self._mem = Memory.from_config(mem0_config)

    def search(self, query: str, agent_id: str, run_id: Optional[str] = None, limit: int = 10) -> List[MemoryResult]:
        kwargs = {"query": query, "agent_id": agent_id, "limit": limit}
        if run_id:
            kwargs["run_id"] = run_id
        results = self._mem.search(**kwargs)
        
        # Depending on mem0 version, results might be a list or a dict
        res_list = results.get("results", []) if isinstance(results, dict) else results
        
        return [MemoryResult(text=r["memory"], score=r.get("score", 0))
                for r in res_list]

    def add(self, content: str, agent_id: str, run_id: Optional[str] = None, metadata: Optional[dict] = None) -> None:
        kwargs = {"agent_id": agent_id, "metadata": metadata or {}}
        if run_id:
            kwargs["run_id"] = run_id
        self._mem.add(content, **kwargs)

    def get_all(self, agent_id: str, limit: int = 100) -> List[MemoryResult]:
        results = self._mem.get_all(agent_id=agent_id, limit=limit)
        res_list = results.get("results", []) if isinstance(results, dict) else results
        return [MemoryResult(text=r["memory"], score=0)
                for r in res_list]

