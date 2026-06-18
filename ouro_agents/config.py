import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

# Re-export RunMode from its canonical home in modes.profiles.
# This avoids circular imports at module load (modes.profiles has no dependency
# on config), while keeping ``from .config import RunMode`` working everywhere.
from .modes.profiles import RunMode  # noqa: F401

# Single source of truth for the memory rhythm vocabulary.
from .memory.naming import Rhythm

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


def merge_openrouter_provider(
    *layers: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Shallow-merge OpenRouter ``provider`` blocks; later layers override."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        merged.update(layer)
    return merged or None


class AgentConfig(BaseModel):
    name: str
    model: str
    workspace: Path = Path("./workspace")
    org_id: Optional[str] = None
    sandbox: "SandboxConfig" = Field(default_factory=lambda: SandboxConfig())
    # Default OpenRouter reasoning for the main agent model (see ``ReasoningConfig``).
    reasoning: Optional[ReasoningConfig] = None


class SandboxConfig(BaseModel):
    """Execution sandbox settings for ``run_python``."""

    mode: Literal["local", "docker"] = "local"
    python_packages: List[str] = Field(default_factory=list)
    image: str = "ouro-agents-sandbox:latest"
    workspace_mount: str = "/workspace"
    network: str = "bridge"
    memory: Optional[str] = "1g"
    cpus: Optional[float] = 1.0
    pids_limit: Optional[int] = 256
    timeout_seconds: int = Field(default=30, ge=1)
    max_output_chars: int = Field(default=50_000, ge=1)
    enable_shell: bool = False
    env_allowlist: List[str] = Field(
        default_factory=lambda: ["OURO_API_KEY", "OURO_BASE_URL"]
    )
    user: Optional[str] = None
    no_new_privileges: bool = True
    drop_capabilities: bool = True


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
    # Overlay on top of ``agent.reasoning`` for heartbeat model builds.
    reasoning: Optional[ReasoningConfig] = None
    # Overlay on top-level ``openrouter_provider`` for heartbeat builds.
    openrouter_provider: Optional[Dict[str, Any]] = None


class MCPServerConfig(BaseModel):
    name: str
    transport: str  # "stdio" or "streamable-http"
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    # One-line summary shown in the tool directory when this server is collapsed
    # to a single entry. Keep it to the capabilities an agent would scan for.
    description: Optional[str] = None


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
    # Memory rhythm: how often the agent rolls its log and runs the dream cycle.
    # Drives both the log bucket window AND the dream cadence (single source of
    # truth — there is intentionally no separate dream cron to misconfigure).
    rhythm: Rhythm = "daily"
    dream_enabled: bool = True
    # Time of day (HH:MM, UTC) for the nightly dream tick. The tick only does
    # work when a new `rhythm` period has begun since the last run.
    dream_time: str = "03:00"
    dream_review_enabled: bool = True
    dream_review_max_per_run: int = 5
    memory_md_max_tokens: int = 4000
    decay_after_days: int = 30
    graph: GraphMemoryConfig = Field(default_factory=GraphMemoryConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_deprecated_dream_schedule(cls, data: Any) -> Any:
        """`dream_schedule` is superseded by `rhythm` + `dream_time`."""
        if isinstance(data, dict) and "dream_schedule" in data:
            import logging

            logging.getLogger(__name__).warning(
                "memory.dream_schedule is deprecated and ignored. The dream cycle "
                "now follows memory.rhythm (daily/weekly/biweekly) and runs at "
                "memory.dream_time."
            )
            data = {k: v for k, v in data.items() if k != "dream_schedule"}
        return data


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    webhook_path: str = "/events"


class EventPoolTimingConfig(BaseModel):
    enabled: bool = True
    settle_seconds: float = Field(default=10.0, ge=0)
    jitter_seconds: float = Field(default=0.0, ge=0)
    max_wait_seconds: float = Field(default=45.0, ge=0)


def _default_event_pool_events() -> Dict[str, EventPoolTimingConfig]:
    return {
        "new-message": EventPoolTimingConfig(
            settle_seconds=2.0,
            jitter_seconds=3.0,
            max_wait_seconds=8.0,
        ),
        "comment": EventPoolTimingConfig(
            settle_seconds=20.0,
            jitter_seconds=20.0,
            max_wait_seconds=90.0,
        ),
        "mention": EventPoolTimingConfig(
            settle_seconds=20.0,
            jitter_seconds=20.0,
            max_wait_seconds=90.0,
        ),
    }


class EventPoolingConfig(BaseModel):
    enabled: bool = True
    events: Dict[str, EventPoolTimingConfig] = Field(
        default_factory=_default_event_pool_events
    )

    @model_validator(mode="before")
    @classmethod
    def merge_default_event_timings(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        configured_events = data.get("events")
        if not isinstance(configured_events, dict):
            return data

        merged = {
            name: timing.model_dump()
            for name, timing in _default_event_pool_events().items()
        }
        for event_type, timing in configured_events.items():
            base = merged.get(event_type, {})
            if isinstance(timing, EventPoolTimingConfig):
                merged[event_type] = {**base, **timing.model_dump()}
            elif isinstance(timing, dict):
                merged[event_type] = {**base, **timing}
            else:
                merged[event_type] = timing

        return {**data, "events": merged}


class PlanningConfig(BaseModel):
    enabled: bool = False
    model: Optional[str] = None
    cadence: str = "1d"
    min_heartbeats: int = 4
    review_window: str = "2h"
    auto_approve: bool = True


class SecurityConfig(BaseModel):
    """Who the agent trusts, and the shared secret guarding ``/run``.

    ``controllers`` and ``trusted`` accept either Ouro usernames or user ids
    (UUIDs), mixed freely. These lists are treated as static input: they are
    never mutated. At startup the agent resolves any usernames to ids (caching
    the lookups) and stores the results in the ``resolved_*`` fields below,
    which is what the runtime authorization checks actually read.
    """

    controllers: List[str] = Field(default_factory=list)
    trusted: List[str] = Field(default_factory=list)
    run_secret: Optional[str] = None

    # Runtime-resolved, never read from / written to config files. Populated by
    # the agent at startup from ``controllers`` / ``trusted``.
    resolved_controller_ids: List[str] = Field(default_factory=list, exclude=True)
    resolved_trusted_ids: List[str] = Field(default_factory=list, exclude=True)
    # First username-form controller entry, used to @mention the controller
    # (e.g. when a plan is ready for review).
    controller_username: Optional[str] = Field(default=None, exclude=True)


def _dedupe_preserve_order(values: Any) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values or []:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _migrate_security_section(expanded_data: dict[str, Any]) -> None:
    """Fold the legacy ``controller`` block and old key names into ``security``.

    Old config shape::

        "controller": {"username": "handle"},
        "security": {
            "controller_user_ids": [...],
            "trusted_user_ids": [...],
            "run_shared_secret": "..."
        }

    becomes::

        "security": {
            "controllers": ["handle", ...],
            "trusted": [...],
            "run_secret": "..."
        }
    """
    security = expanded_data.get("security")
    if not isinstance(security, dict):
        security = {}

    legacy_controller_ids = security.pop("controller_user_ids", None)
    if legacy_controller_ids and not security.get("controllers"):
        security["controllers"] = legacy_controller_ids

    legacy_trusted_ids = security.pop("trusted_user_ids", None)
    if legacy_trusted_ids and not security.get("trusted"):
        security["trusted"] = legacy_trusted_ids

    legacy_secret = security.pop("run_shared_secret", None)
    if legacy_secret is not None and security.get("run_secret") is None:
        security["run_secret"] = legacy_secret

    controller_block = expanded_data.pop("controller", None)
    if isinstance(controller_block, dict):
        legacy_username = controller_block.get("username")
        if legacy_username:
            # Controller username leads so it remains the @mention target.
            security["controllers"] = [legacy_username, *(security.get("controllers") or [])]

    if security.get("controllers"):
        security["controllers"] = _dedupe_preserve_order(security["controllers"])
    if security.get("trusted"):
        security["trusted"] = _dedupe_preserve_order(security["trusted"])

    if security:
        expanded_data["security"] = security


class SubAgentOverride(BaseModel):
    """Per-profile config overrides (e.g. use a different model for the planner)."""
    model: Optional[str] = None
    max_steps: Optional[int] = None
    reasoning: Optional[ReasoningConfig] = None
    openrouter_provider: Optional[Dict[str, Any]] = None


class SubAgentConfig(BaseModel):
    enabled: bool = True
    default_model: Optional[str] = None
    profiles: Dict[str, SubAgentOverride] = Field(default_factory=dict)
    custom_profiles_dir: Optional[str] = None
    parallel_dispatch: bool = True


_MODE_OVERRIDE_ALIASES: dict[str, tuple[str, ...]] = {
    "run": ("autonomous",),
    "planning": ("plan",),
    # ``chat-reply``/``reply`` were merged into the single ``chat`` mode; keep
    # the aliases so existing configs continue to apply to ``chat``.
    "chat-reply": ("chat",),
    "reply": ("chat",),
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
    "openrouter_provider",
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


class ServeProgressConfig(BaseModel):
    enabled: bool = True
    style: Literal["compact", "timeline", "debug"] = "timeline"
    show_spinner: bool = True
    show_prefetch: bool = True
    show_token_updates: bool = True
    show_subagents: bool = True


class DisplayConfig(BaseModel):
    usage_table: UsageTableConfig = Field(default_factory=UsageTableConfig)
    serve_progress: ServeProgressConfig = Field(default_factory=ServeProgressConfig)


class RunLogConfig(BaseModel):
    """Durable SQLite logging of every agent run (``<workspace>/runs.db``).

    Captures a structured record per run — across all modes — plus the full
    step trace, so past runs can be revisited. See ``ouro_agents/run_log.py``.
    """

    enabled: bool = True
    path: Optional[Path] = None  # default: <workspace>/runs.db
    capture_steps: bool = True
    capture_reasoning: bool = True
    capture_observations: bool = True
    max_observation_chars: int = 0  # 0 = unlimited (keep everything)
    capture_subagent_runs: bool = True

    # Agent-facing recall tools (recall_runs / get_run_detail).
    expose_to_agent: bool = True
    agent_default_scope: Literal["team", "conversation", "all"] = "team"
    agent_max_results: int = 10
    agent_max_detail_chars: int = 6000


class RefinementConfig(BaseModel):
    """Dream-phase refinement of agent learnings.

    The dream cycle drains a typed change-set queue (corrections, guidance
    updates, etc.) and uses a cheap model to revise affected workspace docs.
    Asset deletion is handled by the cleanup module and never enters this queue.
    """

    max_changes_per_pass: int = 25
    max_docs_per_pass: int = 15
    window_lines: int = 20
    model: Optional[str] = None


class OuroAgentsConfig(BaseSettings):
    agent: AgentConfig
    # OpenRouter: top-level ``provider`` routing block applied to every model build
    # unless overridden per-profile / per-agent. See
    # https://openrouter.ai/docs/features/provider-routing.
    openrouter_provider: Optional[Dict[str, Any]] = None
    prompt_caching: PromptCachingConfig = Field(default_factory=PromptCachingConfig)
    heartbeat: HeartbeatConfig
    mcp_servers: List[MCPServerConfig]
    memory: MemoryConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    event_pooling: EventPoolingConfig = Field(default_factory=EventPoolingConfig)
    subagents: SubAgentConfig = Field(default_factory=SubAgentConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    modes: ModeConfig = Field(default_factory=ModeConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)
    run_log: RunLogConfig = Field(default_factory=RunLogConfig)
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

        # Migrate legacy per-mode config fields into modes.<name>. The old
        # "chat" key now maps to the single CHAT profile (chat and chat-reply
        # were merged).
        agent_data = expanded_data.get("agent", {})
        legacy_preloads = agent_data.pop("preload_tools", None)
        legacy_max_steps = agent_data.pop("max_steps", None)
        if legacy_preloads or legacy_max_steps:
            modes_data = expanded_data.setdefault("modes", {})
            profiles = modes_data.setdefault("profiles", {})
            if legacy_preloads and isinstance(legacy_preloads, dict):
                for mode_name, tools in legacy_preloads.items():
                    for target in _mode_override_targets(mode_name):
                        entry = profiles.setdefault(target, {})
                        entry.setdefault("preload_tools", tools)
            if legacy_max_steps and isinstance(legacy_max_steps, dict):
                for mode_name, steps in legacy_max_steps.items():
                    for target in _mode_override_targets(mode_name):
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

        # Migrate legacy per-section org_id into the agent-level field.
        agent_section = expanded_data.setdefault("agent", {})
        for section_key in ("memory", "planning"):
            section = expanded_data.get(section_key, {})
            section.pop("team_id", None)
            val = section.pop("org_id", None)
            if val and not agent_section.get("org_id"):
                agent_section["org_id"] = val

        if agent_section.pop("team_id", None):
            raise ValueError(
                "agent.team_id is no longer supported — teams are discovered "
                "at runtime from the platform. Remove the team_id field from "
                "your config.json."
            )

        legacy_python_packages = agent_section.pop("python_packages", None)
        if legacy_python_packages:
            sandbox_section = agent_section.setdefault("sandbox", {})
            if isinstance(sandbox_section, dict):
                sandbox_section.setdefault("python_packages", legacy_python_packages)

        # ``reasoning`` belongs under ``agent``; migrate legacy top-level field.
        legacy_reasoning = expanded_data.pop("reasoning", None)
        if legacy_reasoning and not agent_section.get("reasoning"):
            agent_section["reasoning"] = legacy_reasoning

        _migrate_security_section(expanded_data)

        return cls(**expanded_data)
