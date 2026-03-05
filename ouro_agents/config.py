import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

class AgentConfig(BaseModel):
    name: str
    model: str
    workspace: Path = Path("./workspace")

class HeartbeatConfig(BaseModel):
    enabled: bool = True
    every: str = "30m"
    model: str
    active_hours: Optional[Dict[str, str]] = None

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
    graph: GraphMemoryConfig = Field(default_factory=GraphMemoryConfig)

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000

class OuroAgentsConfig(BaseSettings):
    agent: AgentConfig
    heartbeat: HeartbeatConfig
    mcp_servers: List[MCPServerConfig]
    memory: MemoryConfig
    server: ServerConfig = Field(default_factory=ServerConfig)

    @classmethod
    def load_from_file(cls, path: str | Path) -> "OuroAgentsConfig":
        from dotenv import load_dotenv
        load_dotenv()  # Load environment variables from .env file
        
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
                return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), ''), obj)
            return obj
            
        expanded_data = replace_env_vars(data)
        return cls(**expanded_data)
