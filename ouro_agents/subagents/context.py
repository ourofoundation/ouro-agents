"""Runtime context and result types for subagents.

SubAgentContext carries everything a subagent needs from the parent agent,
passed explicitly rather than requiring access to the OuroAgent instance.

SubAgentResult is the structured return value from run_subagent, replacing
bare strings so callers can distinguish success from failure and inspect
usage metrics.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from ..config import MemoryConfig
    from ..memory import MemoryBackend
    from ..memory.conversation_state import ConversationState


@dataclass
class SubAgentUsage:
    """Token and cost tracking for a single subagent run."""

    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    llm_calls: int = 0
    steps: int = 0
    wall_time_ms: int = 0
    cost_usd: Optional[float] = None
    input_cost_usd: Optional[float] = None
    output_cost_usd: Optional[float] = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    def to_dict(self) -> dict:
        d = {
            "model": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "llm_calls": self.llm_calls,
            "steps": self.steps,
            "wall_time_ms": self.wall_time_ms,
        }
        if self.cost_usd is not None:
            d["cost_usd"] = round(self.cost_usd, 6)
        if self.input_cost_usd is not None:
            d["input_cost_usd"] = round(self.input_cost_usd, 6)
        if self.output_cost_usd is not None:
            d["output_cost_usd"] = round(self.output_cost_usd, 6)
        return d


@dataclass
class SubAgentResult:
    """Structured result from a subagent run."""

    text: str = ""
    success: bool = True
    error: Optional[str] = None
    usage: SubAgentUsage = field(default_factory=SubAgentUsage)

    asset_id: Optional[str] = None
    asset_type: Optional[str] = None
    asset_name: Optional[str] = None
    asset_description: Optional[str] = None

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return self.success and bool(self.text)


@dataclass
class SubAgentContext:
    """Everything a subagent needs from its parent, passed explicitly."""

    workspace: Path
    backend: "MemoryBackend"
    agent_id: str
    memory_config: "MemoryConfig"
    model: Any  # TrackedOpenAIModel or compatible callable
    compactor_model: Any = None

    user_id: Optional[str] = None
    conversation_state: Optional["ConversationState"] = None
    conversation_id: Optional[str] = None
    run_id: str = ""

    # Shared prompt context inherited from the parent agent.
    soul: str = ""
    notes: str = ""
    platform_context: str = ""
    working_memory: str = ""
    user_model: str = ""
    plans_index: str = ""
    doc_store: Any = None
    team_id: Optional[str] = None

    # MCP tool access (populated by OuroAgent._run_subagent for agent-mode subagents)
    deferred_tools: dict = field(default_factory=dict)
    deferred_index: list = field(default_factory=list)

    # Ouro asset refs (UUIDs) to fetch and inject as input context
    asset_refs: list[str] = field(default_factory=list)

    # Memory scoping: tag/category filters limiting which memories are visible.
    # Empty list means no restrictions (full access).
    memory_scopes: list[str] = field(default_factory=list)

    # Pre-authenticated Ouro SDK client for subagents that need run_python
    # with direct platform access (e.g. the developer subagent).
    ouro_client: Any = None

    # Extra Python packages authorized in the sandbox (from config.agent.python_packages).
    python_packages: list[str] = field(default_factory=list)
    python_package_versions: dict = field(default_factory=dict)

    # When set (by OuroAgent), every completed subagent run records usage here
    # (top-level and nested delegate chains share the same ledger).
    record_subagent_usage: Optional[Callable[[str, SubAgentUsage], None]] = None
