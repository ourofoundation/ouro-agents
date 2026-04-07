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
    model: Optional[str] = None
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
    profiles: Dict[str, SubAgentOverride] = Field(default_factory=dict)
    custom_profiles_dir: Optional[str] = None
    parallel_dispatch: bool = True


_MODE_OVERRIDE_ALIASES: dict[str, tuple[str, ...]] = {
    "run": ("autonomous",),
    "planning": ("plan",),
    "chat-reply": ("chat-reply",),
    "reply": ("chat-reply",),
}


def _normalize_mode_name(mode_name: str) -> str:
    return mode_name.strip().lower().replace("_", "-")


def _mode_override_targets(mode_name: str) -> tuple[str, ...]:
    normalized_name = _normalize_mode_name(mode_name)
    return _MODE_OVERRIDE_ALIASES.get(normalized_name, (normalized_name,))


def _normalize_mode_overrides(overrides: Any) -> Any:
    """Normalize user-facing mode aliases to the internal mode names."""
    if not isinstance(overrides, dict):
        return overrides

    normalized: dict[str, Any] = {}
    alias_entries: list[tuple[tuple[str, ...], Any]] = []
    canonical_entries: list[tuple[tuple[str, ...], Any]] = []

    for mode_name, payload in overrides.items():
        raw_name = _normalize_mode_name(mode_name)
        targets = _mode_override_targets(mode_name)
        entry = (targets, payload)
        if targets == (raw_name,):
            canonical_entries.append(entry)
        else:
            alias_entries.append(entry)

    for entries in (alias_entries, canonical_entries):
        for targets, payload in entries:
            for target in targets:
                existing = normalized.get(target)
                if isinstance(existing, dict) and isinstance(payload, dict):
                    normalized[target] = {**existing, **payload}
                else:
                    normalized[target] = payload

    return normalized


def _merge_named_entries(
    base: dict[str, Any], additions: Optional[dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(additions, dict):
        return base

    for name, payload in additions.items():
        existing = base.get(name)
        if isinstance(existing, dict) and isinstance(payload, dict):
            base[name] = {**existing, **payload}
        else:
            base[name] = payload
    return base


def _flatten_named_config_entries(
    section: Any,
    *,
    reserved_keys: set[str],
    container_key: str = "profiles",
    legacy_container_key: str = "overrides",
) -> Any:
    """Collect direct child blocks into a single internal map."""
    if not isinstance(section, dict):
        return section

    flattened: dict[str, Any] = {}
    flattened = _merge_named_entries(flattened, section.pop(legacy_container_key, None))
    flattened = _merge_named_entries(flattened, section.pop(container_key, None))

    for key in list(section.keys()):
        if key in reserved_keys:
            continue
        flattened = _merge_named_entries(flattened, {key: section.pop(key)})

    section[container_key] = flattened
    return section


class ModeOverride(BaseModel):
    """Per-mode config overrides (e.g. change max_steps or preload_tools for a mode)."""
    max_steps: Optional[int] = None
    preload_tools: Optional[List[str]] = None


class ModeConfig(BaseModel):
    """User-level mode config keyed by mode name or friendly alias."""
    profiles: Dict[str, ModeOverride] = Field(default_factory=dict)


_HEARTBEAT_SECTION_KEYS = {
    "enabled",
    "every",
    "model",
    "active_hours",
    "proactive",
    "reasoning",
}

_PLANNING_SECTION_KEYS = {
    "enabled",
    "model",
    "cadence",
    "min_heartbeats",
    "review_window",
    "auto_approve",
}


def _split_mode_profile_fields(
    payload: Any, section_keys: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}, {}

    section_values: dict[str, Any] = {}
    profile_values: dict[str, Any] = {}
    for key, value in payload.items():
        if key in section_keys:
            section_values[key] = value
        else:
            profile_values[key] = value
    return section_values, profile_values


def _promote_special_mode_sections(expanded_data: dict[str, Any]) -> None:
    """Hydrate internal top-level planning/heartbeat config from modes.* blocks."""
    modes_data = expanded_data.get("modes")
    if not isinstance(modes_data, dict):
        return

    profiles = modes_data.get("profiles")
    if not isinstance(profiles, dict):
        return

    for mode_name, target_section, section_keys in (
        ("heartbeat", "heartbeat", _HEARTBEAT_SECTION_KEYS),
        ("plan", "planning", _PLANNING_SECTION_KEYS),
    ):
        payload = profiles.get(mode_name)
        section_values, profile_values = _split_mode_profile_fields(payload, section_keys)
        if section_values:
            section = expanded_data.setdefault(target_section, {})
            if isinstance(section, dict):
                section.update(section_values)
            else:
                expanded_data[target_section] = section_values
        if profile_values:
            profiles[mode_name] = profile_values
        elif mode_name in profiles:
            profiles[mode_name] = {}


class UsageTableConfig(BaseModel):
    show_reasoning: bool = False


class DisplayConfig(BaseModel):
    usage_table: UsageTableConfig = Field(default_factory=UsageTableConfig)


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
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    env_file: Optional[Path] = None

    @classmethod
    def load_from_file(cls, path: str | Path) -> "OuroAgentsConfig":
        import os
        from dotenv import load_dotenv

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r") as f:
            data = json.load(f)

        configured_env_file = data.get("env_file") if isinstance(data, dict) else None
        env_file = os.environ.get("ENV_FILE")
        if not env_file and configured_env_file:
            candidate = Path(configured_env_file).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            env_file = str(candidate)
            data["env_file"] = env_file

        load_dotenv(env_file or ".env", override=True)

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

        # Migrate legacy per-mode config fields into modes.<name>.
        # The old "chat" key mapped to CHAT_REPLY for preloads (CHAT always
        # zeroed preloads), and max_steps.chat was shared by both CHAT and
        # CHAT_REPLY.
        agent_data = expanded_data.get("agent", {})
        legacy_preloads = agent_data.pop("preload_tools", None)
        legacy_max_steps = agent_data.pop("max_steps", None)
        if legacy_preloads or legacy_max_steps:
            modes_data = expanded_data.setdefault("modes", {})
            profiles = modes_data.setdefault("profiles", {})
            if legacy_preloads and isinstance(legacy_preloads, dict):
                for mode_name, tools in legacy_preloads.items():
                    normalized_name = _normalize_mode_name(mode_name)
                    targets = (
                        ["chat-reply"]
                        if normalized_name == "chat"
                        else list(_mode_override_targets(mode_name))
                    )
                    for target in targets:
                        entry = profiles.setdefault(target, {})
                        entry.setdefault("preload_tools", tools)
            if legacy_max_steps and isinstance(legacy_max_steps, dict):
                for mode_name, steps in legacy_max_steps.items():
                    normalized_name = _normalize_mode_name(mode_name)
                    targets = (
                        ["chat", "chat-reply"]
                        if normalized_name == "chat"
                        else list(_mode_override_targets(mode_name))
                    )
                    for target in targets:
                        entry = profiles.setdefault(target, {})
                        entry.setdefault("max_steps", steps)

        modes_data = expanded_data.get("modes")
        if isinstance(modes_data, dict):
            _flatten_named_config_entries(modes_data, reserved_keys={"profiles"})
            modes_data["profiles"] = _normalize_mode_overrides(modes_data.get("profiles"))
            _promote_special_mode_sections(expanded_data)

        subagents_data = expanded_data.get("subagents")
        if isinstance(subagents_data, dict):
            _flatten_named_config_entries(
                subagents_data,
                reserved_keys={
                    "enabled",
                    "default_model",
                    "custom_profiles_dir",
                    "parallel_dispatch",
                    "profiles",
                },
            )

        # Migrate legacy per-section org_id/team_id into agent-level fields.
        agent_section = expanded_data.setdefault("agent", {})
        for section_key in ("memory", "planning"):
            section = expanded_data.get(section_key, {})
            for field in ("org_id", "team_id"):
                val = section.pop(field, None)
                if val and not agent_section.get(field):
                    agent_section[field] = val

        return cls(**expanded_data)
