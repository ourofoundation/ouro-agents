import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# Re-export RunMode from its canonical home in modes.profiles.
# This avoids circular imports at module load (modes.profiles has no dependency
# on config), while keeping ``from .config import RunMode`` working everywhere.
from .modes.profiles import RunMode  # noqa: F401

# OpenRouter unified `reasoning` request field (effort vs max_tokens; model-dependent).
ReasoningEffort = Literal["xhigh", "high", "medium", "low", "minimal", "none"]


class ReasoningConfig(BaseModel):
    """Maps to OpenRouter's top-level ``reasoning`` chat-completions parameter."""

    effort: Optional[ReasoningEffort] = None
    max_tokens: Optional[int] = None
    exclude: Optional[bool] = None
    enabled: Optional[bool] = None


def merge_reasoning(*layers: Optional[ReasoningConfig]) -> Optional[ReasoningConfig]:
    """Later layers override earlier ones for each non-None field."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer is None:
            continue
        merged.update(layer.model_dump(exclude_none=True))
    if not merged:
        return None
    return ReasoningConfig(**merged)


class AgentConfig(BaseModel):
    name: str
    model: str
    workspace: Path = Path("./workspace")
    org_id: Optional[str] = None
    team_id: Optional[str] = None


class PromptCachingConfig(BaseModel):
    enabled: bool = False
    # OpenRouter Anthropic cache TTL options.
    ttl: Literal["5m", "1h"] = "5m"


class ProactiveConfig(BaseModel):
    enabled: bool = False
    servers: List[str] = Field(default_factory=lambda: ["ouro"])


class HeartbeatConfig(BaseModel):
    enabled: bool = True
    every: str = "30m"
    model: str
    active_hours: Optional[Dict[str, str]] = None
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    # Overlay on top-level ``reasoning`` for heartbeat model and other heartbeat=True builds.
    reasoning: Optional[ReasoningConfig] = None


class MCPServerConfig(BaseModel):
    name: str
    transport: str  # "stdio" or "streamable-http"
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None


class GraphMemoryConfig(BaseModel):
    enabled: bool = False
    provider: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class MemoryConfig(BaseModel):
    provider: str = "mem0"
    path: Path = Path("./workspace/memory")
    extraction_model: str
    embedder: str
    search_limit: int = 10
    retrieval_queries: int = 3
    max_retrieval_tokens: int = 4000
    consolidation_enabled: bool = True
    consolidation_schedule: str = "0 3 * * *"
    memory_md_max_tokens: int = 4000
    mid_session_reflection_interval: int = 10
    decay_after_days: int = 30
    graph: GraphMemoryConfig = Field(default_factory=GraphMemoryConfig)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    webhook_path: str = "/events"


class PlanningConfig(BaseModel):
    enabled: bool = False
    cadence: str = "1d"
    min_heartbeats: int = 4
    review_window: str = "2h"
    auto_approve: bool = True


class SubAgentOverride(BaseModel):
    """Per-profile config overrides (e.g. use a different model for the planner)."""
    model: Optional[str] = None
    max_steps: Optional[int] = None
    reasoning: Optional[ReasoningConfig] = None


class SubAgentConfig(BaseModel):
    enabled: bool = True
    default_model: Optional[str] = None
    overrides: Dict[str, SubAgentOverride] = Field(default_factory=dict)
    custom_profiles_dir: Optional[str] = None
    parallel_dispatch: bool = True


class ModeOverride(BaseModel):
    """Per-mode config overrides (e.g. change max_steps or preload_tools for a mode)."""
    max_steps: Optional[int] = None
    preload_tools: Optional[List[str]] = None


class ModeConfig(BaseModel):
    """User-level mode overrides, keyed by mode name (chat, heartbeat, plan, etc.)."""
    overrides: Dict[str, ModeOverride] = Field(default_factory=dict)


class OuroAgentsConfig(BaseSettings):
    agent: AgentConfig
    # OpenRouter: request-level reasoning control (effort / max_tokens / exclude / enabled).
    reasoning: Optional[ReasoningConfig] = None
    prompt_caching: PromptCachingConfig = Field(default_factory=PromptCachingConfig)
    heartbeat: HeartbeatConfig
    mcp_servers: List[MCPServerConfig]
    memory: MemoryConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    subagents: SubAgentConfig = Field(default_factory=SubAgentConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    modes: ModeConfig = Field(default_factory=ModeConfig)

    @classmethod
    def load_from_file(cls, path: str | Path) -> "OuroAgentsConfig":
        from dotenv import load_dotenv

        load_dotenv(override=True)  # Load environment variables from .env file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r") as f:
            data = json.load(f)

        import os
        import re

        def replace_env_vars(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: replace_env_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_env_vars(v) for v in obj]
            elif isinstance(obj, str):
                return re.sub(
                    r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), obj
                )
            return obj

        expanded_data = replace_env_vars(data)

        # Migrate legacy per-mode config fields into modes.overrides.
        # The old "chat" key mapped to CHAT_REPLY (CHAT always zeroed preloads)
        # and max_steps.chat was shared by both CHAT and CHAT_REPLY.
        agent_data = expanded_data.get("agent", {})
        legacy_preloads = agent_data.pop("preload_tools", None)
        legacy_max_steps = agent_data.pop("max_steps", None)
        if legacy_preloads or legacy_max_steps:
            modes_data = expanded_data.setdefault("modes", {})
            overrides = modes_data.setdefault("overrides", {})
            if legacy_preloads and isinstance(legacy_preloads, dict):
                for mode_name, tools in legacy_preloads.items():
                    targets = ["chat-reply"] if mode_name == "chat" else [mode_name]
                    for target in targets:
                        entry = overrides.setdefault(target, {})
                        entry.setdefault("preload_tools", tools)
            if legacy_max_steps and isinstance(legacy_max_steps, dict):
                for mode_name, steps in legacy_max_steps.items():
                    targets = ["chat", "chat-reply"] if mode_name == "chat" else [mode_name]
                    for target in targets:
                        entry = overrides.setdefault(target, {})
                        entry.setdefault("max_steps", steps)

        # Migrate legacy per-section org_id/team_id into agent-level fields.
        agent_section = expanded_data.setdefault("agent", {})
        for section_key in ("memory", "planning"):
            section = expanded_data.get(section_key, {})
            for field in ("org_id", "team_id"):
                val = section.pop(field, None)
                if val and not agent_section.get(field):
                    agent_section[field] = val

        return cls(**expanded_data)
