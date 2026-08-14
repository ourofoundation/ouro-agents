import json
import re
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
# ``max`` is accepted by Moonshot Kimi K3 (thinking always on; only effort it honors).
ReasoningEffort = Literal["xhigh", "high", "medium", "low", "minimal", "none", "max"]
# GPT-5.6+ only (OpenRouter / OpenAI). ``auto`` = model default.
ReasoningContext = Literal["auto", "all_turns", "current_turn"]
# GPT-5.6+ only; ``pro`` routes to the matching ``*-pro`` listing.
ReasoningMode = Literal["standard", "pro"]


class ReasoningConfig(BaseModel):
    """Maps to OpenRouter's top-level ``reasoning`` chat-completions parameter."""

    effort: Optional[ReasoningEffort] = None
    max_tokens: Optional[int] = None
    exclude: Optional[bool] = None
    enabled: Optional[bool] = None
    # GPT-5.6+: which echoed reasoning turns the model may use.
    context: Optional[ReasoningContext] = None
    # GPT-5.6+: standard vs pro multi-pass reasoning.
    mode: Optional[ReasoningMode] = None


def supports_openai_reasoning_context(model_id: str | None) -> bool:
    """True when OpenRouter ``reasoning.context`` / ``reasoning.mode`` apply.

    OpenRouter documents these as GPT-5.6 and newer only. Older OpenAI
    reasoning models and non-OpenAI providers should not receive the fields.
    """
    if not model_id or not model_id.startswith("openai/"):
        return False
    slug = model_id.split("/", 1)[1]
    # gpt-5.6…, gpt-5.10…, gpt-6…, gpt-6.1… (not gpt-5, gpt-5.4, o3, …)
    match = re.match(r"gpt-(\d+)(?:\.(\d+))?", slug)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    if major > 5:
        return True
    return major == 5 and minor >= 6


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


ModelTierName = Literal["strong", "mid", "light"]


class ModelTierSpec(BaseModel):
    """One named model bundle: OpenRouter id + optional reasoning defaults."""

    id: str
    reasoning: Optional[ReasoningConfig] = None
    openrouter_provider: Optional[Dict[str, Any]] = None
    # Cap on completion tokens sent as OpenAI ``max_tokens``. Prevents providers
    # (notably Kimi) from reserving huge default output budgets that trip
    # OpenRouter credit checks (HTTP 402).
    max_completion_tokens: Optional[int] = None


class ModelTiersConfig(BaseModel):
    """Opinionated model roster. Configure once; roles pick a tier.

    ``strong`` — planning, writer, executor, developer (and ``agent.model``
    fallback when mid is unset).
    ``light`` — search, research, reflector, extraction, utilities
    (compaction, summarize, dream, refinement).
    ``mid`` — chat, autonomous, heartbeat, and heartbeat cheap-worker
    ceiling when set; otherwise those roles use ``strong``.
    """

    strong: ModelTierSpec
    light: ModelTierSpec
    mid: Optional[ModelTierSpec] = None


# Role → preferred tier. ``mid`` falls back to ``strong`` when unset.
MODEL_ROLE_TIERS: Dict[str, ModelTierName] = {
    "agent": "strong",
    "planning": "strong",
    "writer": "strong",
    "executor": "strong",
    "developer": "strong",
    "planner": "strong",
    "chat": "mid",
    "autonomous": "mid",
    # Routine ticks run at mid; planning (which drives everything until the
    # next cycle) stays strong. Falls back to strong when mid is unset.
    "heartbeat": "mid",
    "search": "light",
    "research": "light",
    "reflector": "light",
    "utility": "light",
    "extraction": "light",
    "refinement": "light",
}


def tier_spec_for_role(
    tiers: ModelTiersConfig, role: str
) -> ModelTierSpec:
    """Resolve the tier bundle for a harness role (mid → strong if unset)."""
    preferred = MODEL_ROLE_TIERS.get(role, "strong")
    if preferred == "mid" and tiers.mid is None:
        preferred = "strong"
    return getattr(tiers, preferred)


def max_completion_tokens_for_role(
    tiers: Optional[ModelTiersConfig],
    role: str,
) -> Optional[int]:
    """Resolve an optional completion-token cap for a harness role.

    Only an explicit ``models.<tier>.max_completion_tokens`` applies. There
    are no role-level defaults — those truncated high-reasoning outputs more
    often than they helped.
    """
    if tiers is None:
        return None
    tier_cap = tier_spec_for_role(tiers, role).max_completion_tokens
    return int(tier_cap) if tier_cap is not None else None

def _tier_id(models_data: dict[str, Any], name: str) -> Optional[str]:
    spec = models_data.get(name)
    if isinstance(spec, dict):
        model_id = spec.get("id")
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip()
    return None


def _tier_reasoning_payload(
    models_data: dict[str, Any], name: str
) -> Optional[dict[str, Any]]:
    spec = models_data.get(name)
    if not isinstance(spec, dict):
        return None
    reasoning = spec.get("reasoning")
    return reasoning if isinstance(reasoning, dict) else None


def _hydrate_from_model_tiers(expanded_data: dict[str, Any]) -> None:
    """Fill required model fields from ``models`` when callers omit them.

    Explicit ``model`` / ``reasoning`` / ``extraction_model`` values always win.
    Per-subagent models are intentionally left unset so runtime resolution can
    apply the role→tier map.

    Reads tier ``id`` values from the raw dict so a bad ``reasoning`` field
    still hydrates model ids (and the main parse reports the reasoning error
    alone, instead of also failing on missing agent/heartbeat/extraction).
    """
    models_data = expanded_data.get("models")
    if not isinstance(models_data, dict):
        return

    strong_id = _tier_id(models_data, "strong")
    light_id = _tier_id(models_data, "light")
    mid_id = _tier_id(models_data, "mid")
    # Heartbeat runs at mid when a mid tier is configured; planning stays strong.
    heartbeat_id = mid_id or strong_id
    strong_reasoning = _tier_reasoning_payload(models_data, "strong")
    heartbeat_reasoning = (
        _tier_reasoning_payload(models_data, "mid") if mid_id else strong_reasoning
    )

    if not strong_id or not light_id:
        return

    agent = expanded_data.setdefault("agent", {})
    if isinstance(agent, dict):
        agent.setdefault("model", strong_id)
        if agent.get("reasoning") is None and strong_reasoning is not None:
            agent["reasoning"] = strong_reasoning

    memory = expanded_data.setdefault("memory", {})
    if isinstance(memory, dict):
        memory.setdefault("extraction_model", light_id)

    modes = expanded_data.get("modes")
    if not isinstance(modes, dict):
        modes = {}
        expanded_data["modes"] = modes

    heartbeat = modes.get("heartbeat")
    if not isinstance(heartbeat, dict):
        # Allow top-level heartbeat before promotion runs.
        heartbeat = expanded_data.get("heartbeat")
        if not isinstance(heartbeat, dict):
            heartbeat = {}
            modes["heartbeat"] = heartbeat
        else:
            modes.setdefault("heartbeat", heartbeat)
    if heartbeat_id:
        heartbeat.setdefault("model", heartbeat_id)
    if (
        heartbeat.get("reasoning") is None
        and heartbeat_reasoning is not None
    ):
        heartbeat["reasoning"] = heartbeat_reasoning

    planning = modes.get("planning")
    if not isinstance(planning, dict):
        planning = expanded_data.get("planning")
        if not isinstance(planning, dict):
            planning = {}
            modes["planning"] = planning
        else:
            modes.setdefault("planning", planning)
    planning.setdefault("model", strong_id)


class AgentConfig(BaseModel):
    name: str
    model: str
    workspace: Path = Path("./workspace")
    org_id: Optional[str] = None
    sandbox: "SandboxConfig" = Field(default_factory=lambda: SandboxConfig())
    # Default OpenRouter reasoning for the main agent model (see ``ReasoningConfig``).
    # When ``models`` is set, this defaults to ``models.strong.reasoning``.
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
    timeout_seconds: int = Field(default=300, ge=1)
    max_output_chars: int = Field(default=50_000, ge=1)
    enable_shell: bool = False
    env_allowlist: List[str] = Field(
        default_factory=lambda: ["OURO_API_KEY", "OURO_BASE_URL"]
    )
    user: Optional[str] = None
    no_new_privileges: bool = True
    drop_capabilities: bool = True

    def agent_facing_root(self, workspace: Path) -> str:
        """Path string to show the agent as its workspace root.

        In Docker mode this is the container mount (e.g. ``/workspace``), not
        the host absolute path, so prompts match what ``run_python`` sees.
        """
        if self.mode == "docker":
            return self.workspace_mount
        return str(Path(workspace).resolve())


class PromptCachingConfig(BaseModel):
    enabled: bool = False
    # OpenRouter Anthropic cache TTL options.
    ttl: Literal["5m", "1h"] = "5m"


class ChatCompactionConfig(BaseModel):
    """Watermark-anchored chat history compaction.

    Soft compaction runs after a successful reply (background). Hard compaction
    runs synchronously before a reply when the estimated prompt would exceed
    ``hard_fraction`` of ``context_tokens``.
    """

    enabled: bool = True
    # Assumed model context window for threshold math (estimate-based).
    context_tokens: int = Field(default=100_000, ge=8_000)
    soft_fraction: float = Field(default=0.60, ge=0.1, le=0.95)
    hard_fraction: float = Field(default=0.85, ge=0.2, le=0.98)
    # Verbatim turns kept out of a soft/hard fold so the tail stays hot.
    keep_recent_turns: int = Field(default=8, ge=0, le=64)

    @model_validator(mode="after")
    def _soft_before_hard(self) -> "ChatCompactionConfig":
        if self.soft_fraction >= self.hard_fraction:
            raise ValueError("chat_compaction.soft_fraction must be < hard_fraction")
        return self


class ObservationPolicyConfig(BaseModel):
    """Spill oversized tool results; keep in-run history append-only for cache.

    Results over ``max_inline_chars`` are written to
    ``scratch/tool-outputs/<run_id>/`` and replaced with a head/tail stub before
    they enter agent memory. Older steps are rewritten only on a rare one-shot
    compact when cumulative observation chars cross ``run_compact_ceiling``.

    ``exempt_tools`` never spill — use for tools whose payload *is* the context
    the agent asked for (skill bodies via ``load_skill``).
    """

    max_inline_chars: int = Field(default=20_000, ge=500)
    head_chars: int = Field(default=2_500, ge=0)
    tail_chars: int = Field(default=1_500, ge=0)
    max_step_chars: int = Field(default=40_000, ge=500)
    run_compact_ceiling: int = Field(default=160_000, ge=1_000)
    keep_recent_steps: int = Field(default=3, ge=1, le=20)
    excerpt_chars: int = Field(default=1_200, ge=100)
    exempt_tools: List[str] = Field(default_factory=lambda: ["load_skill"])


class CuriosityConfig(BaseModel):
    """Wind-down beats reserved for self-directed exploration.

    When enabled, the final ``last_beats`` heartbeats of each active window
    run as curiosity ticks: the priority ladder and quest inbox are set aside
    and the agent works from its CURIOSITY.md playbook instead.
    """

    enabled: bool = False
    last_beats: int = Field(default=3, ge=1)


class HeartbeatConfig(BaseModel):
    enabled: bool = True
    every: str = "30m"
    model: str
    active_hours: Optional[Dict[str, str]] = None
    curiosity: CuriosityConfig = Field(default_factory=CuriosityConfig)
    # MCP servers the main heartbeat may load. Search access belongs to the
    # ``search`` / ``research`` subagents, so the default is Ouro only.
    servers: List[str] = Field(default_factory=lambda: ["ouro"])
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

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "MCPServerConfig":
        transport = (self.transport or "").strip().lower()
        if transport not in {"stdio", "streamable-http"}:
            raise ValueError(
                f"MCP server {self.name!r}: transport must be 'stdio' or "
                f"'streamable-http', got {self.transport!r}"
            )
        self.transport = transport
        if transport == "stdio":
            if not self.command:
                raise ValueError(
                    f"MCP server {self.name!r}: 'command' is required for stdio transport"
                )
        elif transport == "streamable-http":
            if not self.url:
                raise ValueError(
                    f"MCP server {self.name!r}: 'url' is required for "
                    "streamable-http transport"
                )
        return self


class GraphMemoryConfig(BaseModel):
    enabled: bool = False
    provider: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class MemoryConfig(BaseModel):
    provider: str = "mem0"
    path: Path = Path("./workspace/protected/memory")
    extraction_model: str
    embedder: str
    # Default top-K per memory_recall query (per-query ``limit`` overrides).
    search_limit: int = 10
    # Global soft cap (in tokens, ~4 chars each) on a single memory_recall's
    # combined output across all its queries.
    max_retrieval_tokens: int = 4000
    # Relevance floor for recall results (memory_signal_score). Results below
    # this are dropped unless the caller passed explicit filters. The best hit
    # is always kept so recall never comes back empty when matches exist.
    min_signal_score: float = 0.35
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
    # Public HTTPS origin for this agent (nginx front door), e.g.
    # "https://agents.ouro.foundation/apollo". Used by agent routes, and
    # available for any other feature that needs the externally reachable URL.
    public_base_url: Optional[str] = None


class EventPoolTimingConfig(BaseModel):
    enabled: bool = True
    settle_seconds: float = Field(default=10.0, ge=0)
    jitter_seconds: float = Field(default=0.0, ge=0)
    max_wait_seconds: float = Field(default=45.0, ge=0)


def _default_event_pool_events() -> Dict[str, EventPoolTimingConfig]:
    return {
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


def _default_notification_inbox_categories() -> List[str]:
    return ["mentions", "comments", "shares"]


class NotificationInboxConfig(BaseModel):
    """Settings for the heartbeat Notification Inbox digest."""

    expire_after_hours: int = Field(default=72, ge=1)
    max_threads: int = Field(default=15, ge=1)
    max_fetch: int = Field(default=100, ge=1, le=200)
    snippet_chars: int = Field(default=150, ge=20)
    # Backend notification categories included in the digest.
    categories: List[str] = Field(
        default_factory=_default_notification_inbox_categories
    )


_ALWAYS_REALTIME_EVENTS = frozenset(
    {"interrupt", "asset.deleted", "new-conversation"}
)


class EventDeliveryConfig(BaseModel):
    """Per-event-type delivery mode.

    ``realtime`` runs the agent on webhook receipt (with pooling if configured).
    ``heartbeat`` acknowledges the webhook without running and leaves the
    notification unread for the next heartbeat's Notification Inbox.

    When ``realtime_for_controllers`` is true (default), controllers bypass
    heartbeat deferral so their comments/mentions still wake the agent
    immediately.
    """

    events: Dict[str, Literal["realtime", "heartbeat"]] = Field(default_factory=dict)
    realtime_for_controllers: bool = True
    notification_inbox: NotificationInboxConfig = Field(
        default_factory=NotificationInboxConfig
    )

    @model_validator(mode="after")
    def _validate_events(self) -> "EventDeliveryConfig":
        from ouro.events import WEBHOOK_EVENT_TYPES

        for name, mode in self.events.items():
            if name not in WEBHOOK_EVENT_TYPES:
                raise ValueError(f"Unknown event type in event_delivery: {name}")
            if mode == "heartbeat" and name in _ALWAYS_REALTIME_EVENTS:
                raise ValueError(f"Event '{name}' must stay realtime")
        return self

    def mode_for(self, event_type: str) -> str:
        return self.events.get(event_type, "realtime")

    def should_defer_to_heartbeat(
        self,
        event_type: str,
        *,
        actor_user_id: Optional[str] = None,
        controller_user_ids: Optional[List[str]] = None,
    ) -> bool:
        """Whether this event should be deferred to the heartbeat inbox."""
        if self.mode_for(event_type) != "heartbeat":
            return False
        if (
            self.realtime_for_controllers
            and actor_user_id
            and controller_user_ids
            and actor_user_id in controller_user_ids
        ):
            return False
        return True


class PlanningConfig(BaseModel):
    enabled: bool = False
    model: Optional[str] = None
    cadence: str = "1d"
    review_window: str = "2h"
    auto_approve: bool = True
    # Global caps across all teams for one agent process.
    max_plans_per_day: int = 2
    # Open owned-quest items (including waiting/parked) that block new plans.
    backlog_limit: int = 8


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


class AskControllerConfig(BaseModel):
    """Controller-question workflow for uncertain or consequential actions."""

    enabled: bool = True
    fast_wait_seconds: float = Field(default=90.0, ge=0.0, le=240.0)
    gate_mode: Literal["off", "observe"] = "observe"


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
    "servers",
    "proactive",  # legacy; migrated to ``servers`` at load time
    "reasoning",
    "openrouter_provider",
    "curiosity",
}


def _migrate_heartbeat_proactive(expanded_data: dict[str, Any]) -> None:
    """Flatten legacy ``heartbeat.proactive`` into ``heartbeat.servers``.

    Heartbeats are inherently proactive. The old ``enabled`` flag was ambiguous
    and ``proactive.servers`` was often computed but not enforced. Canonical
    shape is a single ``servers`` allowlist.
    """
    candidates: list[dict[str, Any]] = []
    top = expanded_data.get("heartbeat")
    if isinstance(top, dict):
        candidates.append(top)
    modes = expanded_data.get("modes")
    if isinstance(modes, dict):
        hb = modes.get("heartbeat")
        if isinstance(hb, dict):
            candidates.append(hb)
        profiles = modes.get("profiles")
        if isinstance(profiles, dict):
            profile_hb = profiles.get("heartbeat")
            if isinstance(profile_hb, dict):
                candidates.append(profile_hb)

    for section in candidates:
        proactive = section.pop("proactive", None)
        if not isinstance(proactive, dict):
            continue
        if "servers" in section and isinstance(section.get("servers"), list):
            continue
        if proactive.get("enabled") is False:
            section.setdefault("servers", ["ouro"])
            continue
        servers = proactive.get("servers")
        if isinstance(servers, list) and servers:
            # Main heartbeat owns Ouro; search is delegated to subagents.
            section["servers"] = ["ouro"] if "ouro" in servers else list(servers)
        else:
            section.setdefault("servers", ["ouro"])

_PLANNING_SECTION_KEYS = {
    "enabled",
    "model",
    "cadence",
    # Retired knob, still routed here so legacy configs don't leak it into
    # the plan mode profile (PlanningConfig ignores it).
    "min_heartbeats",
    "review_window",
    "auto_approve",
    "max_plans_per_day",
    "backlog_limit",
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


class AgentRoutesConfig(BaseModel):
    """Agent-authored routes: draft handlers as tools, publish as Ouro services."""

    enabled: bool = False
    path_prefix: str = "/routes"
    serve_token_env: str = "AGENT_ROUTES_SERVE_TOKEN"
    request_timeout_seconds: int = Field(default=120, ge=1, le=600)
    max_concurrent_requests: int = Field(default=2, ge=1)


class OuroAgentsConfig(BaseSettings):
    agent: AgentConfig
    # Named model roster. When set, roles pick strong/mid/light by default and
    # per-mode / per-subagent ``model`` fields become optional overrides.
    models: Optional[ModelTiersConfig] = None
    # OpenRouter: top-level ``provider`` routing block applied to every model build
    # unless overridden per-profile / per-agent. See
    # https://openrouter.ai/docs/features/provider-routing.
    openrouter_provider: Optional[Dict[str, Any]] = None
    prompt_caching: PromptCachingConfig = Field(default_factory=PromptCachingConfig)
    chat_compaction: ChatCompactionConfig = Field(default_factory=ChatCompactionConfig)
    observations: ObservationPolicyConfig = Field(default_factory=ObservationPolicyConfig)
    heartbeat: HeartbeatConfig
    mcp_servers: List[MCPServerConfig]
    memory: MemoryConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    event_pooling: EventPoolingConfig = Field(default_factory=EventPoolingConfig)
    event_delivery: EventDeliveryConfig = Field(default_factory=EventDeliveryConfig)
    subagents: SubAgentConfig = Field(default_factory=SubAgentConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    ask_controller: AskControllerConfig = Field(default_factory=AskControllerConfig)
    modes: ModeConfig = Field(default_factory=ModeConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)
    run_log: RunLogConfig = Field(default_factory=RunLogConfig)
    agent_routes: AgentRoutesConfig = Field(default_factory=AgentRoutesConfig)
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

        # Apply model-tier defaults before other migrations so agent.model /
        # heartbeat.model / extraction_model are present for required fields.
        _hydrate_from_model_tiers(expanded_data)
        _migrate_heartbeat_proactive(expanded_data)

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
