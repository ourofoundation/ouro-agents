import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class RunMode(str, Enum):
    CHAT = "chat"
    AUTONOMOUS = "autonomous"
    HEARTBEAT = "heartbeat"


class ToolPreloadConfig(BaseModel):
    """Tools to auto-preload per run mode, saving a load_tool call."""
    chat: List[str] = Field(default_factory=lambda: ["ouro:send_message"])
    autonomous: List[str] = Field(default_factory=list)
    heartbeat: List[str] = Field(default_factory=list)


class MainAgentMaxStepsConfig(BaseModel):
    """Max tool-calling loop steps for the main smolagents agent (default is 20)."""

    chat: int = 20
    autonomous: int = 20
    heartbeat: int = 20


class AgentConfig(BaseModel):
    name: str
    model: str
    workspace: Path = Path("./workspace")
    preload_tools: ToolPreloadConfig = Field(default_factory=ToolPreloadConfig)
    max_steps: MainAgentMaxStepsConfig = Field(default_factory=MainAgentMaxStepsConfig)


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
    memory_md_max_tokens: int = 4000
    mid_session_reflection_interval: int = 10
    decay_after_days: int = 30
    graph: GraphMemoryConfig = Field(default_factory=GraphMemoryConfig)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class PlanningConfig(BaseModel):
    enabled: bool = False
    cadence: str = "1d"
    min_heartbeats: int = 4
    review_window: str = "2h"
    auto_approve: bool = True
    team_id: Optional[str] = None
    org_id: Optional[str] = None


class SubAgentOverride(BaseModel):
    """Per-profile config overrides (e.g. use a different model for the planner)."""
    model: Optional[str] = None
    max_steps: Optional[int] = None


class SubAgentConfig(BaseModel):
    enabled: bool = True
    default_model: Optional[str] = None
    overrides: Dict[str, SubAgentOverride] = Field(default_factory=dict)
    custom_profiles_dir: Optional[str] = None
    parallel_dispatch: bool = True


class OuroAgentsConfig(BaseSettings):
    agent: AgentConfig
    prompt_caching: PromptCachingConfig = Field(default_factory=PromptCachingConfig)
    heartbeat: HeartbeatConfig
    mcp_servers: List[MCPServerConfig]
    memory: MemoryConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    subagents: SubAgentConfig = Field(default_factory=SubAgentConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)

    @classmethod
    def load_from_file(cls, path: str | Path) -> "OuroAgentsConfig":
        from dotenv import load_dotenv

        load_dotenv(override=True)  # Load environment variables from .env file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r") as f:
            data = json.load(f)

        # Environment variable expansion could be handled here if needed,
        # but pydantic-settings also handles some of it.
        # For explicit ${VAR} replacement in JSON strings:
        import os
        import re

        def replace_env_vars(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: replace_env_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_env_vars(v) for v in obj]
            elif isinstance(obj, str):
                # Replace ${VAR} with os.environ.get('VAR', '')
                return re.sub(
                    r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), obj
                )
            return obj

        expanded_data = replace_env_vars(data)
        return cls(**expanded_data)
