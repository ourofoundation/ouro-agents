"""Subagent profile definitions.

A SubAgentProfile defines how a subagent behaves: its system prompt,
tool access, and resource limits. Every subagent runs as a full
ToolCallingAgent loop — no special "pipeline" or "template" modes.

Built-in profiles cover the common cases. Custom profiles can be loaded
from JSON/YAML files in the workspace ``subagents/`` directory or a path
set via ``config.subagents.custom_profiles_dir``.

Profiles marked ``delegatable=True`` appear in the main agent's subagent
directory and can be invoked at runtime via the ``delegate`` tool.
"""

import json
import logging
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .preflight import HEARTBEAT_PREFLIGHT_PROMPT, PREFLIGHT_PROMPT
from .prompts import (
    CONTEXT_LOADER_PROMPT,
    DEVELOPER_PROMPT,
    EXECUTOR_PROMPT,
    PLANNER_PROMPT,
    RESEARCH_PROMPT,
    WRITER_PROMPT,
)
from .reflector import REFLECTOR_PROMPT

logger = logging.getLogger(__name__)

# Maps to smolagents ``LogLevel`` (OFF, ERROR, INFO, DEBUG). Default ``off`` keeps subagents quiet.
SubagentLogLevel = Literal["off", "error", "info", "debug"]


class SubAgentProfile(BaseModel):
    """Defines how a subagent behaves."""

    name: str
    description: str = ""

    system_prompt: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_servers: list[str] = Field(default_factory=list)
    can_load_mcp_tools: bool = False
    delegatable: bool = False
    max_steps: int = 5
    model_override: Optional[str] = None
    default_return_mode: Literal["summary_only", "full_text", "auto"] = "summary_only"

    # MCP tools to preload for the subagent (resolved from deferred_tools)
    preload_tools: list[str] = Field(default_factory=list)

    # Subagent chaining: names of other delegatable profiles this subagent
    # can invoke. Empty means no chaining.
    can_delegate_to: list[str] = Field(default_factory=list)

    # Memory scoping: restrict vector memory searches to entries matching
    # these category tags. Empty means full access.
    memory_scopes: list[str] = Field(default_factory=list)

    # When True, the runner injects `run_python` (with the Ouro SDK client if
    # available) into this subagent's tool list.
    needs_python_tool: bool = False

    # Composable skill names resolved at runtime from builtin_skills/ or
    # workspace/skills/. Each name maps to a .md file whose body is injected
    # into the subagent's task context.
    skills: list[str] = Field(default_factory=list)

    # OuroLogger level for this subagent's ToolCallingAgent loop. ``off`` matches
    # the previous quiet behavior; use ``info`` or ``debug`` for visible traces.
    subagent_log_level: SubagentLogLevel = "off"


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

PREFLIGHT = SubAgentProfile(
    name="preflight",
    description="Classify task, gather relevant memory context, and optionally sketch an execution plan.",
    system_prompt=PREFLIGHT_PROMPT,
    allowed_tools=["memory_recall", "ouro:get_asset"],
    max_steps=20,
    memory_scopes=[],
    subagent_log_level="info",
)

HEARTBEAT_PREFLIGHT = SubAgentProfile(
    name="heartbeat_preflight",
    description="Decide what the agent should focus on during an autonomous heartbeat.",
    system_prompt=HEARTBEAT_PREFLIGHT_PROMPT,
    allowed_tools=["memory_recall"],
    max_steps=3,
    memory_scopes=[],
    subagent_log_level="info",
)

CONTEXT_LOADER = SubAgentProfile(
    name="context_loader",
    description="Load internal memory, entity files, and task context into a concise briefing.",
    system_prompt=CONTEXT_LOADER_PROMPT,
    allowed_tools=["memory_recall"],
    max_steps=3,
    memory_scopes=[],
)

REFLECTOR = SubAgentProfile(
    name="reflector",
    description="Curate long-term memories from recent conversation turns.",
    system_prompt=REFLECTOR_PROMPT,
    allowed_tools=["memory_recall"],
    max_steps=7,
)

# Delegate-able profiles

RESEARCH = SubAgentProfile(
    name="research",
    description=(
        "Investigate a topic using web search. Runs multiple queries, "
        "cross-references sources, and saves a full research post on Ouro."
    ),
    system_prompt=RESEARCH_PROMPT,
    allowed_tools=["memory_recall"],
    allowed_servers=["search"],
    can_load_mcp_tools=True,
    preload_tools=["search:tavily_search", "ouro:create_post"],
    delegatable=True,
    max_steps=12,
    can_delegate_to=["writer"],
    skills=["ouro_markdown", "asset_output"],
)

PLANNER = SubAgentProfile(
    name="planner",
    description="Generate a short numbered execution plan for a complex task.",
    system_prompt=PLANNER_PROMPT,
    allowed_tools=["memory_recall"],
    delegatable=True,
    max_steps=3,
)

EXECUTOR = SubAgentProfile(
    name="executor",
    description=(
        "Execute a focused sub-task using tools. Has access to MCP tools and "
        "memory. Use for self-contained actions you want off your main context."
    ),
    system_prompt=EXECUTOR_PROMPT,
    allowed_tools=["memory_recall"],
    can_load_mcp_tools=True,
    preload_tools=["ouro:create_post"],
    delegatable=True,
    max_steps=8,
    can_delegate_to=["writer"],
    skills=["ouro_markdown", "asset_output"],
)

WRITER = SubAgentProfile(
    name="writer",
    description=(
        "Draft polished, high-value posts and text documents from goals, notes, "
        "and context. Saves the draft as an Ouro post and returns a brief handoff."
    ),
    system_prompt=WRITER_PROMPT,
    allowed_tools=[],
    can_load_mcp_tools=True,
    preload_tools=["ouro:create_post"],
    delegatable=True,
    max_steps=5,
    skills=["ouro_markdown", "asset_output"],
)

DEVELOPER = SubAgentProfile(
    name="developer",
    description=(
        "Execute complex workflows using the Ouro Python SDK (ouro-py) directly. "
        "Use for batch operations, data pipelines, multi-step API interactions, "
        "and anything that benefits from programmatic control over the platform."
    ),
    system_prompt=DEVELOPER_PROMPT,
    allowed_tools=["memory_recall"],
    can_load_mcp_tools=True,
    preload_tools=["ouro:create_post"],
    delegatable=True,
    max_steps=12,
    needs_python_tool=True,
    skills=["ouro_py", "ouro_markdown", "asset_output"],
)

# All built-in profiles
PROFILES = [
    PREFLIGHT,
    CONTEXT_LOADER,
    RESEARCH,
    PLANNER,
    REFLECTOR,
    EXECUTOR,
    WRITER,
    DEVELOPER,
]

# All built-in profiles by name (for merging with custom)
_BUILTIN_PROFILES: dict[str, SubAgentProfile] = {p.name: p for p in PROFILES}

# Registry of profiles the main agent can delegate to at runtime.
DELEGATABLE_PROFILES: dict[str, SubAgentProfile] = {
    p.name: p for p in PROFILES if p.delegatable
}


# ---------------------------------------------------------------------------
# Custom profile loading (JSON / YAML from workspace)
# ---------------------------------------------------------------------------


def _try_load_yaml(path: Path) -> dict:
    """Attempt to parse a YAML file. Falls back to JSON if pyyaml unavailable."""
    try:
        import yaml

        return yaml.safe_load(path.read_text()) or {}
    except ImportError:
        logger.warning("pyyaml not installed — cannot load %s; use JSON instead", path)
        return {}


def _load_profile_file(path: Path) -> Optional[SubAgentProfile]:
    """Load a single profile from a JSON or YAML file."""
    try:
        if path.suffix in (".yaml", ".yml"):
            data = _try_load_yaml(path)
        elif path.suffix == ".json":
            data = json.loads(path.read_text())
        else:
            return None

        if not data or not isinstance(data, dict):
            return None

        if "name" not in data:
            data["name"] = path.stem

        # Drop legacy fields from custom profiles written for older schemas
        for legacy_key in (
            "mode",
            "compress_output",
            "summary_max_tokens",
            "max_output_tokens",
            "prefer_artifact_output",
            "create_artifact",
            "read_artifact",
            "list_artifacts",
        ):
            data.pop(legacy_key, None)

        return SubAgentProfile(**data)
    except Exception as e:
        logger.warning("Failed to load custom profile from %s: %s", path, e)
        return None


def load_custom_profiles(directory: Path) -> dict[str, SubAgentProfile]:
    """Load SubAgentProfile definitions from JSON/YAML files in a directory.

    Files should contain a single profile definition. The filename (without
    extension) is used as the profile name if ``name`` is not specified in
    the file.

    Custom profiles override built-in profiles of the same name.
    """
    profiles: dict[str, SubAgentProfile] = {}
    if not directory.exists():
        return profiles

    for path in sorted(directory.iterdir()):
        if path.suffix not in (".json", ".yaml", ".yml"):
            continue
        if path.name.startswith(".") or path.name.startswith("_"):
            continue
        profile = _load_profile_file(path)
        if profile:
            profiles[profile.name] = profile
            logger.info(
                "Loaded custom subagent profile: %s from %s", profile.name, path
            )

    return profiles


def build_profile_registry(
    custom_dir: Optional[Path] = None,
) -> dict[str, SubAgentProfile]:
    """Build the full delegatable profile registry, merging built-in + custom.

    Custom profiles override built-in profiles of the same name.
    Returns only profiles with ``delegatable=True``.
    """
    merged = dict(_BUILTIN_PROFILES)

    if custom_dir:
        custom = load_custom_profiles(custom_dir)
        merged.update(custom)

    return {name: p for name, p in merged.items() if p.delegatable}


def get_all_profiles(
    custom_dir: Optional[Path] = None,
) -> dict[str, SubAgentProfile]:
    """Return all profiles (delegatable or not), merging built-in + custom."""
    merged = dict(_BUILTIN_PROFILES)
    if custom_dir:
        custom = load_custom_profiles(custom_dir)
        merged.update(custom)
    return merged
