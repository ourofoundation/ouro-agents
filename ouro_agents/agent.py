import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from uuid import uuid4

from smolagents import (
    ActionStep,
    ChatMessageStreamDelta,
    FinalAnswerStep,
    ToolCollection,
    tool,
)

from .artifacts import PrefetchSpec, resolve_prefetch
from .classify import is_trivial_message
from .cancellation import RunCancellationToken, RunCancelled
from .config import (
    MCPServerConfig,
    OuroAgentsConfig,
    ReasoningConfig,
    RunMode,
    max_completion_tokens_for_role,
    merge_openrouter_provider,
    merge_reasoning,
    supports_openai_reasoning_context,
    tier_spec_for_role,
)
from .conversation_naming import (
    await_conversation_naming,
    start_name_conversation_if_needed,
)
from .constants import openrouter_attribution_headers
from .controller_questions import ControllerQuestionManager, ControllerReplyResolution
from .provider_reasoning import first_party_provider_slug
from .display import OuroDisplay, create_logger, get_display
from .memory import create_memory_backend
from .memory import DocStore
from .memory.naming import (
    current_period_heading,
    memory_team_id,
    period_key,
    store_rhythm,
)
from .memory.ouro_docs import CompositeDocStore, LocalDocStore, OuroDocStore
from .memory.reflection import (
    store_reflection_memories,
    validated_daily_log_entries,
    write_log,
)
from .memory.tools import make_memory_tools
from .teams import TeamContext, TeamRegistry

from .modes import (
    ModeProfile,
    apply_capability_envelope,
    apply_mode_override,
    resolve_mode_profile,
)
from .modes.framing import ASK_CONTROLLER_GUIDANCE
from .observer import AgentObserver, ProgressEvent, emit_progress
from .mcp_http import ManagedMcpProcess, spawn_managed_mcp_http
from .mcp_locking import McpServerLocks, wrap_mcp_tool_with_lock
from .run_context import (
    ActiveRunRegistry,
    RunContext,
    bind_run_context,
    get_run_context,
)
from .chat_telemetry import (
    apply_usage as apply_chat_turn_usage,
    build_chat_turn_record,
    format_chat_turn,
)
from .chat_compaction import (
    build_injectable_history,
    estimate_chat_prompt_tokens,
    load_compaction,
    run_compaction_locked,
    should_compact,
)
from .run_log import ChatTurnRecord, RunLogStore, RunRecord
from .security.policy import Capability, CapabilityEnvelope
from .security.tool_capabilities import (
    filter_deferred_by_servers,
    filter_deferred_excluding,
    filter_deferred_tools,
)
from .skills import get_skill_directory, load_startup_skills
from .soul import build_prompt
from .subagents.context import SubAgentUsage
from .subagents.delegate_utils import (
    delegate_success_payload,
    dispatch_delegate_tasks,
    dumps_delegate_result,
    normalize_return_mode,
    resolve_auto_return_mode,
    summarize_delegate_text,
    validate_delegate_result,
)
from .subagents.reflector import (
    ReflectionResult,
    build_run_reflection_task,
    normalize_daily_log_entry,
    parse_reflection_result,
)
from .tool_prompt import build_tool_calling_system_prompt
from .tools.agent_base import SanitizedToolCallingAgent as _SanitizedToolCallingAgent
from .tools.agent_route_tools import make_publish_route_tools, make_run_coil_tool
from .tools.python_tool import make_code_tools
from .tools.run_history_tools import make_run_history_tools
from .tools.scheduler_tools import make_scheduler_tools
from .tools.skills_tools import make_load_skill_tool
from .provider_errors import (
    RATE_LIMIT_NOTE,
    RATE_LIMIT_NOTE_MIN_DELAY_S,
    format_rate_limit_activity,
)
from .usage import (
    MirroredUsageTracker,
    RunUsage,
    TrackedOpenAIModel,
    UsageTracker,
    collect_run_usage,
    format_usage_breakdown,
)
from .utils.callbacks import build_step_callback
from .utils.conversation import (
    HISTORY_FETCH_LIMIT,
    append_conversation_turn,
    build_history_steps,
    conversation_file,
    extract_run_steps,
    extract_tool_summary,
    format_conversation_turns,
    load_conversation_turns,
    resolve_conversation_turns,
)
from .utils.debug import (
    append_run_debug_markdown_trace,
    write_run_debug_markdown_preamble,
)
from .utils.streaming import FinalAnswerStreamer, IntermediateContentStreamer
from .uuid_v7 import uuid7_str

if TYPE_CHECKING:
    from .subagents.context import SubAgentContext

logger = logging.getLogger(__name__)


def _dedup_bullet_lines(text: str) -> str:
    """Drop exact-duplicate bullet lines, keeping first occurrence.

    Automatic memory extraction accumulates repeats of the same fact,
    preference, or log entry across runs. Deduping at render time keeps the
    prompt lean without touching the stored documents. Only "- " bullets are
    considered — headings, prose, and blank lines pass through untouched.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines():
        normalized = " ".join(line.split())
        if normalized.startswith("- "):
            if normalized in seen:
                continue
            seen.add(normalized)
        lines.append(line)
    return "\n".join(lines)


RunStatusCallback = Callable[[str, Optional[str], bool], None]
RunResponseCallback = Callable[[str], None]


class OuroAgent:
    def __init__(self, config: OuroAgentsConfig):
        self.config = config
        soul_path = config.agent.workspace / "SOUL.md"
        self.soul = soul_path.read_text() if soul_path.exists() else ""
        notes_path = config.agent.workspace / "NOTES.md"
        self.notes = notes_path.read_text() if notes_path.exists() else ""
        self.skills = load_startup_skills(config)
        self.skill_directory = get_skill_directory(config)
        # Fallback tracker/ledger used when no RunContext is bound (startup
        # model build, memory backend, heartbeat pre-reset). Live runs bind a
        # fresh RunContext so overlapping modes do not share these.
        self._usage_tracker = UsageTracker()
        self._workspace = config.agent.workspace

        # Migrate harness paths into protected/ BEFORE opening Chroma / runs.db,
        # otherwise create_memory_backend mkdir's an empty protected/memory and
        # the migrate step skips the real store.
        from .tools.workspace_paths import (
            migrate_protected_workspace,
            protected_data,
            protected_runs_db,
        )

        migrate_protected_workspace(self._workspace)

        self.memory = create_memory_backend(
            config.memory,
            usage_tracker=self._usage_tracker,
        )
        self._subagent_ledger: list[tuple[str, SubAgentUsage]] = []
        self.model = self._build_model(config.agent.model)

        # Durable run logging (SQLite). Per-run ids live on RunContext; tick_id
        # is still set on the agent for the duration of a heartbeat cycle.
        run_log_path = config.run_log.path or protected_runs_db(self._workspace)
        self._run_log = RunLogStore(run_log_path, enabled=config.run_log.enabled)
        self._controller_questions = ControllerQuestionManager(
            agent_name=config.agent.name,
            org_id=config.agent.org_id or "",
            controller_ids=lambda: list(
                self.config.security.resolved_controller_ids or []
            ),
            own_user_id=lambda: self.own_user_id,
            ouro_client=self._get_ouro_client,
            store=self._run_log,
            fast_wait_seconds=config.ask_controller.fast_wait_seconds,
            remember_direction=self._remember_controller_direction,
        )
        self._current_tick_id: Optional[str] = None

        self._mcp_contexts: list = []
        self._managed_mcp: list[ManagedMcpProcess] = []
        self._mcp_locks = McpServerLocks()
        self._deferred_tools: dict = {}
        self._deferred_tools_by_raw_name: dict = {}
        self._deferred_index: list[dict] = []
        self._server_descriptions: dict[str, str] = {}
        self._mcp_connected = False
        self._own_user_id: Optional[str] = None
        self._active_runs_lock = threading.RLock()
        self._active_run_tokens: set[RunCancellationToken] = set()
        self._active_runs = ActiveRunRegistry()

        self.team_registry: TeamRegistry = TeamRegistry.from_platform_context(
            self._workspace,
            config.agent.org_id,
        )
        self._team_doc_stores: dict[str, DocStore] = {}

        from .memory.log_prefix_migration import migrate_log_prefix_workspace
        from .memory.team_paths import migrate_workspace_team_dirs

        migrate_log_prefix_workspace(self._workspace)
        moves = migrate_workspace_team_dirs(self._workspace)
        if moves:
            logger.info(
                "Migrated %d team dir(s) to slug names: %s",
                len(moves),
                ", ".join(moves),
            )

        self.doc_store: DocStore = CompositeDocStore(
            local=LocalDocStore(
                workspace=config.agent.workspace,
                agent_name=config.agent.name,
                rhythm=config.memory.rhythm,
            ),
            ouro=None,
        )

        from .scheduler import AgentScheduler

        self.scheduler = AgentScheduler(
            protected_data(config.agent.workspace) / "scheduled_tasks.json"
        )

        self._load_custom_profiles()
        self._python_package_versions = self._validate_python_packages()

    def reset_usage_tracking(self) -> None:
        """Reset usage accumulators for the active run (or agent fallback)."""
        self._active_usage_tracker().reset()
        self.memory.reset_usage()
        self._active_subagent_ledger().clear()

    def _active_usage_tracker(self) -> UsageTracker:
        ctx = get_run_context()
        if ctx is not None and ctx.usage_tracker is not None:
            return ctx.usage_tracker
        return self._usage_tracker

    def _active_subagent_ledger(self) -> list[tuple[str, SubAgentUsage]]:
        ctx = get_run_context()
        if ctx is not None:
            return ctx.subagent_ledger
        return self._subagent_ledger

    def _active_run_id(self) -> Optional[str]:
        ctx = get_run_context()
        return ctx.run_id if ctx is not None else None

    # Backward-compatible alias for call sites / tests that still read the
    # old instance field name.
    @property
    def _current_run_id(self) -> Optional[str]:
        return self._active_run_id()

    def _get_heartbeat_cheap_workers(self) -> bool:
        ctx = get_run_context()
        return bool(ctx.heartbeat_cheap_workers) if ctx is not None else False

    def _set_heartbeat_cheap_workers(self, value: bool) -> None:
        ctx = get_run_context()
        if ctx is not None:
            ctx.heartbeat_cheap_workers = value

    def _validate_python_packages(self) -> dict[str, str | None]:
        """Validate configured python_packages at startup and return version map."""
        packages = self.config.agent.sandbox.python_packages
        if not packages:
            return {}
        if self.config.agent.sandbox.mode == "docker":
            from .tools.docker_sandbox import validate_python_packages_in_docker

            try:
                versions = validate_python_packages_in_docker(
                    packages,
                    config=self.config.agent.sandbox,
                    workspace=self.config.agent.workspace,
                    agent_name=self.config.agent.name,
                )
            except Exception as e:
                logger.warning(
                    "Failed to validate python packages in Docker sandbox image %s: %s",
                    self.config.agent.sandbox.image,
                    e,
                )
                return {p: None for p in packages}
        else:
            from .tools.python_tool import validate_python_packages

            versions = validate_python_packages(packages)

        missing = [p for p, v in versions.items() if v is None]
        if missing:
            target = (
                f"Docker image {self.config.agent.sandbox.image}"
                if self.config.agent.sandbox.mode == "docker"
                else "the agent's Python environment"
            )
            logger.warning(
                "Missing python packages: %s — install them in %s to enable in the sandbox",
                ", ".join(missing),
                target,
            )
        return versions

    def _load_custom_profiles(self) -> None:
        """Build this instance's delegatable profile registry.

        Starts from the built-in set and layers workspace / configured custom
        profiles on top. The registry lives on ``self.delegatable_profiles``
        (no module-level mutation) so multiple ``OuroAgent`` instances in the
        same process don't leak profiles into each other — matters for tests
        and for future multi-tenant servers.
        """
        from .subagents.profiles import build_profile_registry

        custom_dir = None
        if self.config.subagents.custom_profiles_dir:
            custom_dir = Path(self.config.subagents.custom_profiles_dir)
            if not custom_dir.is_absolute():
                custom_dir = self.config.agent.workspace / custom_dir
        else:
            default_dir = self.config.agent.workspace / "subagents"
            if default_dir.exists():
                custom_dir = default_dir

        self.delegatable_profiles = build_profile_registry(custom_dir)
        logger.info(
            "Profile registry: %d delegatable profiles (%s)",
            len(self.delegatable_profiles),
            ", ".join(self.delegatable_profiles.keys()),
        )

    @property
    def own_user_id(self) -> Optional[str]:
        """The agent's platform user ID, populated after MCP connect."""
        return self._own_user_id

    def _refresh_platform_context(self) -> None:
        """Fetch profile, org, and team info via the Ouro SDK and cache it.

        Called at startup and on heartbeat. Other runs read from cache.
        """
        context: dict = {
            "profile": None,
            "organizations": [],
            "teams": [],
            "base_url": os.getenv("OURO_FRONTEND_URL")
            or os.getenv("OURO_BASE_URL")
            or "https://ouro.foundation",
        }

        ouro = self._get_ouro_client()
        if ouro is None:
            logger.warning("Platform context: Ouro SDK client unavailable")
        else:
            try:
                profile = ouro.users.me() or {}
                context["profile"] = {
                    "id": str(profile.get("user_id") or getattr(ouro.user, "id", "")),
                    "username": profile.get("username"),
                    "display_name": profile.get("display_name"),
                    "email": profile.get("email") or getattr(ouro.user, "email", None),
                    "bio": profile.get("bio"),
                }
            except Exception as e:
                logger.warning("Platform context: failed to fetch profile: %s", e)

            try:
                context["organizations"] = [
                    {
                        "id": str(org.get("id") or ""),
                        "name": org.get("name"),
                        "display_name": org.get("display_name"),
                        "role": (org.get("membership") or {}).get("role"),
                    }
                    for org in ouro.organizations.list()
                ]
            except Exception as e:
                logger.warning("Platform context: failed to fetch orgs: %s", e)

            try:
                context["teams"] = [
                    self._team_context_entry(team)
                    for team in ouro.teams.list(joined=True)
                ]
            except Exception as e:
                logger.warning("Platform context: failed to fetch teams: %s", e)

        from .platform_context_prompt import platform_context_path

        cache_path = platform_context_path(self._workspace)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._own_user_id = (context.get("profile") or {}).get("id")
        self._resolve_security_actors()
        context["controllers"] = self._controller_context_entries()

        cache_path.write_text(json.dumps(context, indent=2))

        self.team_registry.refresh(context, self.config.agent.org_id)
        logger.info(
            "Refreshed platform context: %d orgs, %d teams, %d controllers",
            len(context["organizations"]),
            len(self.team_registry.team_ids()),
            len(context["controllers"]),
        )
        if self._mcp_connected:
            self._init_doc_store()

    def _controller_context_entries(self) -> list[dict]:
        """Build controller username/user_id pairs for platform context prompts."""
        security = self.config.security
        id_cache = self._load_security_id_cache()
        entries: list[dict] = []
        seen: set[str] = set()

        for raw_entry in security.controllers or []:
            entry = str(raw_entry or "").strip()
            if not entry:
                continue
            if self._looks_like_user_id(entry):
                if entry not in seen:
                    seen.add(entry)
                    entries.append({"user_id": entry})
                continue

            username = entry.lstrip("@")
            user_id = id_cache.get(username)
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            entries.append({"username": username, "user_id": user_id})

        for user_id in security.resolved_controller_ids or []:
            if user_id not in seen:
                seen.add(user_id)
                entries.append({"user_id": user_id})

        return entries

    @staticmethod
    def _team_context_entry(team) -> dict:
        """Flatten an SDK Team into the cached platform-context shape."""
        org = team.get("organization") or {}
        source_policy = (
            team.get("source_policy") or org.get("source_policy") or "any"
        )
        membership = team.get("userMembership") or {}
        desc = team.get("description")
        return {
            "id": str(team.get("id") or ""),
            "name": team.get("name"),
            "slug": team.get("slug"),
            "org_id": str(team.get("org_id") or ""),
            "organization_name": org.get("name") or org.get("display_name"),
            "role": membership.get("role"),
            "source_policy": source_policy,
            "agent_can_create": source_policy != "web_only",
            "description": desc.get("text", "") if isinstance(desc, dict) else desc,
        }

    def _resolve_security_actors(self) -> None:
        """Resolve ``security.controllers`` / ``security.trusted`` to user ids.

        Entries may be Ouro usernames or user ids (UUIDs), mixed. Ids pass
        through unchanged; usernames are looked up via ``search_users`` and
        cached so subsequent startups skip the network round-trip. The original
        config lists are never mutated — results land in the ``resolved_*``
        fields the authorization layer reads.
        """
        security = self.config.security
        cache = self._load_security_id_cache()

        controller_ids, controller_username = self._resolve_actor_entries(
            security.controllers, cache
        )
        trusted_ids, _ = self._resolve_actor_entries(security.trusted, cache)

        security.resolved_controller_ids = controller_ids
        security.resolved_trusted_ids = trusted_ids
        security.controller_username = controller_username

        self._save_security_id_cache(cache)
        logger.info(
            "Resolved security actors: %d controller id(s), %d trusted id(s)",
            len(controller_ids),
            len(trusted_ids),
        )

    @staticmethod
    def _looks_like_user_id(value: str) -> bool:
        from uuid import UUID

        try:
            UUID(value)
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    def _resolve_actor_entries(
        self, entries: list[str], cache: dict[str, str]
    ) -> tuple[list[str], Optional[str]]:
        """Return (resolved_ids, first_username) for a list of actor entries."""
        resolved: list[str] = []
        seen: set[str] = set()
        first_username: Optional[str] = None

        for raw_entry in entries or []:
            entry = str(raw_entry or "").strip()
            if not entry:
                continue
            if self._looks_like_user_id(entry):
                if entry not in seen:
                    seen.add(entry)
                    resolved.append(entry)
                continue

            username = entry.lstrip("@")
            if first_username is None:
                first_username = username

            user_id = cache.get(username) or self._lookup_user_id(username)
            if not user_id:
                logger.warning("Could not resolve username '%s' to a user id", username)
                continue
            cache[username] = user_id
            if user_id not in seen:
                seen.add(user_id)
                resolved.append(user_id)

        return resolved, first_username

    def _lookup_user_id(self, username: str) -> Optional[str]:
        ouro = self._get_ouro_client()
        if ouro is None:
            return None
        try:
            candidates = ouro.users.search(username)
        except Exception as e:
            logger.warning("Failed to resolve username '%s': %s", username, e)
            return None

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_username = str(candidate.get("username") or "").strip()
            candidate_id = str(
                candidate.get("user_id") or candidate.get("id") or ""
            ).strip()
            if candidate_username == username and candidate_id:
                logger.info("Resolved username '%s' to user id %s", username, candidate_id)
                return candidate_id
        return None

    def _security_cache_path(self) -> Path:
        from .tools.workspace_paths import protected_data

        return protected_data(self._workspace) / "security_resolved.json"

    def _load_security_id_cache(self) -> dict[str, str]:
        path = self._security_cache_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.warning("Failed to read security id cache: %s", e)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if k and v}

    def _save_security_id_cache(self, cache: dict[str, str]) -> None:
        if not cache:
            return
        path = self._security_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache, indent=2, sort_keys=True))
        except Exception as e:
            logger.warning("Failed to write security id cache: %s", e)

    def _load_platform_context(self) -> str:
        """Load cached platform context for inclusion in the system prompt."""
        from .platform_context_prompt import format_platform_context_for_prompt

        return format_platform_context_for_prompt(self._workspace)

    def _sorted_team_ids(self) -> list[str]:
        if not self.team_registry:
            return []
        return sorted(self.team_registry.team_ids())

    def _resolve_doc_store(
        self,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
    ) -> DocStore:
        if doc_store is not None:
            return doc_store
        if team_id:
            return self.doc_store_for(team_id)
        return self.doc_store

    def _load_working_memory(
        self,
        *,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
    ) -> str:
        """Load working memory and today's daily log for the system prompt.

        When running in a team-scoped context, also loads the root-level
        MEMORY.md as shared cross-team knowledge.
        """
        active_doc_store = self._resolve_doc_store(team_id=team_id, doc_store=doc_store)
        parts: list[str] = []
        name = self.config.agent.name
        rhythm = store_rhythm(active_doc_store)
        period = period_key(rhythm)
        memory_name = active_doc_store.memory_name(name)
        log_name = active_doc_store.log_name(name, period)

        content = active_doc_store.read(memory_name)
        if content:
            parts.append(content)
        log_content = active_doc_store.read(log_name)
        if log_content:
            parts.append(
                f"## {current_period_heading(rhythm)} ({period})\n{log_content}"
            )

        if team_id:
            shared_memory = self._load_shared_memory()
            if shared_memory:
                parts.append(f"## Shared Memory (cross-team)\n{shared_memory}")

        return _dedup_bullet_lines("\n\n".join(parts))

    def _load_shared_memory(self) -> str:
        """Load the root-level MEMORY.md for cross-team shared knowledge.

        Routed through the no-team doc store via the ``SHARED:memory``
        identity prefix so the doc-store abstraction stays the only path
        from agent code to disk.
        """
        return self.doc_store.read("SHARED:memory")

    def _load_scheduled_task_awareness(self) -> str:
        """Return a compact, read-only summary of scheduled tasks."""
        tasks = self.scheduler.list_tasks()
        if not tasks:
            return ""

        enabled_tasks = [task for task in tasks if task.enabled]
        disabled_count = len(tasks) - len(enabled_tasks)

        lines = [
            "## Scheduled Tasks",
            "These run on their own cadence. Use them as context only; do not manage or execute them during heartbeat.",
        ]

        if enabled_tasks:
            for task in enabled_tasks[:8]:
                status = task.last_run_status or "never-run"
                scope = f" team={task.team_id}" if task.team_id else " shared"
                lines.append(
                    f"- {task.name} [{task.schedule} {task.timezone}] status={status} runs={task.run_count}{scope}"
                )
            remaining = len(enabled_tasks) - min(len(enabled_tasks), 8)
            if remaining > 0:
                lines.append(f"- ... and {remaining} more enabled scheduled task(s)")
        else:
            lines.append("- No enabled scheduled tasks.")

        if disabled_count:
            lines.append(f"- Disabled scheduled tasks: {disabled_count}")

        return "\n".join(lines)

    def _load_shared_prompt_context(
        self,
        *,
        user_id: Optional[str] = None,
        include_scheduled_tasks: bool = False,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
    ) -> dict[str, str]:
        """Load the common prompt context shared by main and subagent runs.

        Catch-all All/nil ``team_id`` is remapped to untargeted for memory
        loading only; callers keep the original id for provenance.
        """
        scope_team_id = memory_team_id(team_id)
        active_doc_store = self._resolve_doc_store(
            team_id=scope_team_id, doc_store=doc_store
        )
        working_memory_parts = [
            self._load_working_memory(
                team_id=scope_team_id, doc_store=active_doc_store
            )
        ]
        if include_scheduled_tasks:
            working_memory_parts.append(self._load_scheduled_task_awareness())
        working_memory = "\n\n".join(part for part in working_memory_parts if part)

        user_model_text = ""
        if user_id:
            from .memory.user_model import strip_empty_sections

            user_model_text = strip_empty_sections(
                active_doc_store.read(f"USER:{user_id}")
            )
        notes_name = f"NOTES:{self.config.agent.name}"
        notes_text = active_doc_store.read(notes_name)
        if not notes_text and not scope_team_id:
            notes_text = self.notes

        plans_index_text = self._own_quests_index()

        return {
            "soul": self.soul,
            "notes": notes_text,
            "platform_context": self._load_platform_context(),
            "working_memory": working_memory,
            "user_model": user_model_text,
            "plans_index": plans_index_text,
        }

    def _cross_team_recent_activity_digest(self) -> str:
        """Compact tails of current period logs across teams for chat status."""
        from .memory.context_loader import build_cross_team_recent_activity

        labels: dict[str, str] = {}
        registry = getattr(self, "team_registry", None)
        if registry is not None:
            get_team = getattr(registry, "get_team", None)
            for tid in sorted(registry.team_ids()):
                team = get_team(tid) if callable(get_team) else None
                labels[tid] = (
                    getattr(team, "slug", None)
                    or getattr(team, "name", None)
                    or tid[:8]
                )
        rhythm = getattr(self.config.memory, "rhythm", None) or "weekly"
        return build_cross_team_recent_activity(
            self.config.agent.workspace,
            period=period_key(rhythm),
            team_labels=labels,
            rhythm=rhythm,
        )

    _OWN_QUESTS_INDEX_TTL_SECS = 300

    def _own_quests_cached(self) -> list:
        """Agent's own quests, cached briefly to avoid per-turn search hits."""
        import time

        cached = getattr(self, "_own_quests_list_cache", None)
        now = time.monotonic()
        if cached and now - cached[0] < self._OWN_QUESTS_INDEX_TTL_SECS:
            return cached[1]

        from .modes.planning import search_own_quests

        quests = search_own_quests(self, limit=10)
        self._own_quests_list_cache = (now, quests)
        return quests

    def _own_quests_index(self, *, pointer: bool = False) -> str:
        """Short prompt index of the agent's own quests.

        Chat uses a one-line pointer so quest titles do not steer the thread;
        other modes get the full id list.
        """
        from .modes.planning import (
            format_quests_index_for_prompt,
            format_quests_index_pointer,
        )

        quests = self._own_quests_cached()
        if pointer:
            return format_quests_index_pointer(quests)
        return format_quests_index_for_prompt(quests)

    def _is_anthropic_model(self, model_id: str) -> bool:
        return model_id.startswith("anthropic/")

    # Alibaba's commercial Qwen line (``-plus`` / ``-max`` / ``-flash``, with an
    # optional ``-preview`` snapshot) is served exclusively by DashScope, which
    # supports Anthropic-style ``cache_control`` markers. Dated snapshots
    # (e.g. ``qwen3.5-plus-02-15``) and middle-qualifier variants
    # (``-coder-``, ``-vl-``) do NOT support explicit caching, so they are
    # excluded. Open-source Qwen ids route across providers that mostly ignore
    # the markers and are excluded too.
    _QWEN_EXPLICIT_CACHE_RE = re.compile(
        r"^qwen/qwen\d+(?:\.\d+){0,2}-(?:plus|max|flash)(?:-preview)?$"
    )

    def _supports_explicit_cache(self, model_id: str) -> bool:
        """Models that need per-message ``cache_control`` breakpoints.

        Unlike Anthropic (where OpenRouter honors a top-level ``cache_control``
        field), Alibaba/Qwen only caches when the markers are injected into the
        message content blocks. ``TrackedOpenAIModel`` does that injection.
        """
        return bool(self._QWEN_EXPLICIT_CACHE_RE.match(model_id))

    def _model_id_for_role(self, role: str) -> Optional[str]:
        """OpenRouter model id for a harness role from ``models`` tiers, if set."""
        tiers = self.config.models
        if tiers is None:
            return None
        return tier_spec_for_role(tiers, role).id

    def _utility_model_id(self) -> str:
        """Cheap model for compaction / summarize / dream / similar utilities."""
        return (
            self._model_id_for_role("utility")
            or self.config.heartbeat.model
            or self.config.agent.model
        )

    def _resolve_reasoning(
        self,
        *,
        subagent_profile: Optional[str] = None,
        heartbeat: bool = False,
        role: Optional[str] = None,
    ) -> Optional[ReasoningConfig]:
        """Merge tier defaults + optional heartbeat/subagent overlays.

        With ``models`` configured, the role's tier supplies the base reasoning
        (so light roles do not inherit ``strong`` effort). Without tiers, keep
        the legacy cascade: ``agent.reasoning`` → heartbeat → subagent.
        """
        effective_role = (
            role
            or subagent_profile
            or ("heartbeat" if heartbeat else "agent")
        )
        tiers = self.config.models
        layers: list[Optional[ReasoningConfig]]
        if tiers is not None:
            layers = [tier_spec_for_role(tiers, effective_role).reasoning]
            if effective_role == "agent":
                layers.append(self.config.agent.reasoning)
        else:
            layers = [self.config.agent.reasoning]
            if heartbeat or effective_role in {
                "heartbeat",
                "utility",
                "extraction",
                "refinement",
            }:
                layers.append(self.config.heartbeat.reasoning)

        if heartbeat or effective_role == "heartbeat":
            if tiers is not None:
                layers.append(self.config.heartbeat.reasoning)
        if subagent_profile:
            override = self.config.subagents.profiles.get(subagent_profile)
            if override and override.reasoning is not None:
                layers.append(override.reasoning)
        return merge_reasoning(*layers)

    def _resolve_openrouter_provider(
        self,
        *,
        subagent_profile: Optional[str] = None,
        heartbeat: bool = False,
        role: Optional[str] = None,
    ) -> Optional[dict]:
        """Merge global + tier + optional heartbeat/subagent provider overlays."""
        effective_role = (
            role
            or subagent_profile
            or ("heartbeat" if heartbeat else "agent")
        )
        layers: list[Optional[dict]] = [self.config.openrouter_provider]
        tiers = self.config.models
        if tiers is not None:
            tier_provider = tier_spec_for_role(tiers, effective_role).openrouter_provider
            if tier_provider:
                layers.append(tier_provider)
        if heartbeat or effective_role == "heartbeat":
            layers.append(self.config.heartbeat.openrouter_provider)
        if subagent_profile:
            override = self.config.subagents.profiles.get(subagent_profile)
            if override and override.openrouter_provider is not None:
                layers.append(override.openrouter_provider)
        return merge_openrouter_provider(*layers)

    def _build_openrouter_extra_body(
        self,
        model_id: str,
        reasoning: Optional[ReasoningConfig],
        provider: Optional[dict] = None,
    ) -> Optional[dict]:
        body: dict = {}
        cfg = self.config.prompt_caching
        if cfg.enabled and self._is_anthropic_model(model_id):
            cache_control: dict[str, str] = {"type": "ephemeral"}
            if cfg.ttl == "1h":
                cache_control["ttl"] = "1h"
            body["cache_control"] = cache_control

        if reasoning is not None:
            r = reasoning.model_dump(exclude_none=True)
            if model_id.startswith("moonshotai/"):
                # Kimi K3 has thinking always on and accepts only effort "max".
                # Map other efforts away; keep exclude / explicit max.
                # GPT-5.6 context/mode do not apply here.
                exclude = r.get("exclude")
                r = {"enabled": True}
                if exclude is not None:
                    r["exclude"] = exclude
                if reasoning.effort == "max":
                    r["effort"] = "max"
            elif supports_openai_reasoning_context(model_id):
                # GPT-5.6+: when we echo reasoning_details in tool loops, prefer
                # all_turns so the model can continue prior chain-of-thought.
                # Explicit config (including "auto" / "current_turn") wins.
                if "context" not in r:
                    r["context"] = "all_turns"
            else:
                # Older OpenAI / non-OpenAI providers: do not send GPT-5.6-only
                # fields (OpenRouter documents stripping mode, but not context).
                r.pop("context", None)
                r.pop("mode", None)
            if r:
                body["reasoning"] = r

        # MiniMax M-series does interleaved thinking and, by default, injects the
        # model's chain-of-thought into the ``content`` channel as
        # ``reasoning_content``. That mixing is what makes MiniMax leak raw
        # tool-call tokens (``<invoke …>`` / ``]<]minimax[>[``) into assistant
        # text instead of emitting structured tool_calls. ``reasoning_split``
        # asks for the thinking to be separated into ``reasoning_details`` so the
        # content channel stays clean. See the MiniMax M3 tool-use guide:
        # https://platform.minimax.io/docs/guides/text-m3-function-call
        # (Harmless if the route ignores the flag.)
        if model_id.startswith("minimax/"):
            body["reasoning_split"] = True

        # Families whose format="unknown" reasoning we replay must never route
        # across providers (the blocks are provider-specific). Hard-pin them to
        # their first-party endpoint, overriding config-level fallbacks.
        pin_slug = first_party_provider_slug(model_id)
        if pin_slug:
            provider = {
                **(provider or {}),
                "order": [pin_slug],
                "allow_fallbacks": False,
            }

        if provider:
            body["provider"] = provider

        return body if body else None

    def _default_tool_choice(
        self,
        model_id: str,
        *,
        reasoning: Optional[ReasoningConfig] = None,
        conversational: bool = False,
        heartbeat: bool = False,
    ) -> Optional[str]:
        # Conversational (chat) runs must be free to answer a casual message
        # with plain content and no tool call. smolagents' default
        # `tool_choice="required"` forces a pointless tool call on every step
        # of a greeting, so chat always uses `auto`.
        if conversational:
            return "auto"
        # Heartbeat OUTPUT FORMAT requires a plain JSON final message with no
        # tool calls. `required` traps interleaved-thinking models in a
        # finish-loop (noop shell/memory calls) that can burn the full step
        # budget — and with slow provider calls, block the next hourly tick.
        if heartbeat:
            return "auto"
        # Some upstream providers reject smolagents' default `tool_choice="required"`:
        #   - MiniMax routes return a 400 outright.
        #   - DeepSeek's own "DeepSeek" provider serves reasoning-enabled models
        #     as `deepseek-reasoner`, which rejects `required` with
        #     ``deepseek-reasoner does not support this tool_choice``. Even the
        #     non-reasoning chat variants are happy with `auto`, so we apply it
        #     to the whole family rather than try to detect reasoning at the
        #     call site.
        #   - Qwen via Alibaba rejects `required` (and object tool_choice) in
        #     thinking mode. Qwen 3.x routes often enter thinking even without an
        #     explicit OpenRouter ``reasoning`` block, so always use ``auto``.
        #   - Zhipu GLM (``z-ai/``): the first-party Z.AI endpoint advertises no
        #     route for ``tool_choice="required"`` (OpenRouter returns "no
        #     endpoints found"), so we'd be forced off the canonical provider.
        #     ``auto`` works there and is healthier for an interleaved-thinking
        #     model, which should be free to finish with a plain content reply.
        #   - GPT-5.6+ via OpenRouter: same interleaved-thinking finish path;
        #     `required` prevents the plain-content terminal reply our modes use.
        if (
            model_id.startswith("minimax/")
            or model_id.startswith("deepseek/")
            or model_id.startswith("qwen/")
            or model_id.startswith("z-ai/")
            or supports_openai_reasoning_context(model_id)
        ):
            return "auto"
        return None

    def _build_model(
        self,
        model_id: str,
        *,
        reasoning: Optional[ReasoningConfig] = None,
        subagent_profile: Optional[str] = None,
        heartbeat: bool = False,
        role: Optional[str] = None,
        usage_tracker: Optional[UsageTracker] = None,
        conversational: bool = False,
        max_completion_tokens: Optional[int] = None,
    ) -> TrackedOpenAIModel:
        model_kwargs = {}
        effective_role = (
            role
            or subagent_profile
            or ("heartbeat" if heartbeat else "agent")
        )
        resolved = (
            reasoning
            if reasoning is not None
            else self._resolve_reasoning(
                subagent_profile=subagent_profile,
                heartbeat=heartbeat,
                role=role,
            )
        )
        provider = self._resolve_openrouter_provider(
            subagent_profile=subagent_profile,
            heartbeat=heartbeat,
            role=role,
        )
        extra_body = self._build_openrouter_extra_body(model_id, resolved, provider)
        if extra_body:
            model_kwargs["extra_body"] = extra_body
        tool_choice = self._default_tool_choice(
            model_id,
            reasoning=resolved,
            conversational=conversational,
            heartbeat=heartbeat,
        )
        if tool_choice is not None:
            model_kwargs["tool_choice"] = tool_choice

        completion_cap = max_completion_tokens
        if completion_cap is None:
            completion_cap = max_completion_tokens_for_role(
                self.config.models, effective_role
            )
        if completion_cap is not None:
            model_kwargs["max_tokens"] = completion_cap

        cache_cfg = self.config.prompt_caching
        explicit_cache = cache_cfg.enabled and self._supports_explicit_cache(model_id)

        return TrackedOpenAIModel(
            model_id=model_id,
            api_base="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            tracker=usage_tracker or self._active_usage_tracker(),
            reasoning_callback=get_display().reasoning,
            cache_breakpoints=explicit_cache,
            cache_ttl=cache_cfg.ttl,
            client_kwargs={
                "default_headers": openrouter_attribution_headers(),
            },
            **model_kwargs,
        )

    def _get_ouro_client(self):
        """Build or return a cached Ouro SDK client for runtime platform calls.

        Uses the same env vars as the MCP server (OURO_API_KEY, OURO_BASE_URL).
        Returns None if credentials are unavailable so callers degrade
        gracefully.
        """
        if hasattr(self, "_ouro_client"):
            return self._ouro_client

        api_key = os.getenv("OURO_API_KEY")
        if not api_key:
            logger.warning("OURO_API_KEY not set — Ouro SDK unavailable")
            self._ouro_client = None
            return None

        try:
            from ouro import Ouro

            self._ouro_client = Ouro(
                api_key=api_key,
                base_url=os.getenv("OURO_BASE_URL"),
            )
            logger.info("Ouro SDK client created")
        except Exception as e:
            logger.warning("Failed to create Ouro SDK client: %s", e)
            self._ouro_client = None

        return self._ouro_client

    def _remember_controller_direction(
        self,
        direction: str,
        *,
        run_id: str = "",
        team_id: Optional[str] = None,
    ) -> bool:
        """Persist a settled controller answer as durable work-direction memory."""
        from .memory.focus import remember_work_direction

        return remember_work_direction(
            getattr(self, "memory", None),
            self.config.agent.name,
            direction,
            source="controller-decision",
            run_id=run_id,
            team_id=team_id,
            strength=0.95,
            text_prefix="Controller decision",
        )

    def _resolve_conversation_turns(
        self,
        conversation_id: str,
        *,
        limit: int = 24,
        trigger_turn_id: Optional[str] = None,
    ) -> list[dict]:
        exclude_turn_ids = {trigger_turn_id} if trigger_turn_id else None
        return resolve_conversation_turns(
            self._workspace,
            conversation_id,
            ouro_client=self._get_ouro_client(),
            agent_user_id=self.own_user_id,
            exclude_turn_ids=exclude_turn_ids,
            limit=limit,
        )

    def _summarize_turns(self, turns: list[dict]) -> str:
        """Compress older conversation turns into a brief summary via LLM."""
        condensed = []
        for turn in turns:
            role = str(turn.get("role", "unknown")).lower()
            content = str(turn.get("content", ""))[:300]
            condensed.append(f"{role}: {content}")
        blob = "\n".join(condensed)

        try:
            summary_model = self._build_model(
                self._utility_model_id(),
                role="utility",
            )
            result = summary_model(
                [
                    {
                        "role": "user",
                        "content": (
                            "Summarize this conversation excerpt in 2-3 sentences. "
                            "Capture key topics, decisions, and any open questions. "
                            "Be concise.\n\n"
                            f"{blob}"
                        ),
                    }
                ],
            )
            logger.info("Summarized conversation")
            return result.content if hasattr(result, "content") else str(result)
        except Exception as e:
            logger.warning("Conversation summarization failed: %s", e)
            return f"({len(turns)} earlier messages about: {blob[:200]}...)"

    def _maybe_soft_compact_chat(
        self,
        *,
        conversation_id: str,
        all_turns: list[dict],
        history_turns: list[dict],
        history_summary: str,
        system_prompt: str,
        dynamic_context: str,
        task: str,
    ) -> None:
        """Background soft compaction when a chat turn sat near the soft budget."""
        cfg = self.config.chat_compaction
        if not cfg.enabled:
            return
        est = estimate_chat_prompt_tokens(
            system_prompt=system_prompt,
            dynamic_context=dynamic_context,
            task=task,
            injected_turns=history_turns,
            summary=history_summary,
        )
        level = should_compact(
            est,
            context_tokens=cfg.context_tokens,
            soft_fraction=cfg.soft_fraction,
            hard_fraction=cfg.hard_fraction,
        )
        if level is None:
            return
        # Soft path only here — hard already ran synchronously before the reply.
        if level == "hard":
            # Still over after the reply's history shape; fold again with keep_recent.
            reason = "hard-post"
        else:
            reason = "soft"

        utility = self._build_model(self._utility_model_id(), role="utility")
        run_compaction_locked(
            self._workspace,
            conversation_id,
            all_turns,
            utility,
            reason=reason,
            keep_recent=cfg.keep_recent_turns,
            model_id=self._utility_model_id(),
        )

    def _init_doc_store(self) -> None:
        """Initialize or refresh per-team OuroDocStore instances.

        ``self.doc_store`` remains the local shared-memory store. Team-specific
        runs must pass ``team_id`` and resolve through ``doc_store_for``.
        """
        agent_cfg = self.config.agent
        if not agent_cfg.org_id:
            logger.warning("OuroDocStore: org_id not configured — teams require org_id")
            return

        team_ids = self._sorted_team_ids()
        if not team_ids:
            logger.warning(
                "OuroDocStore: no teams discovered from platform — check network and org membership"
            )
            return

        client = self._get_ouro_client()
        stale_team_ids = set(self._team_doc_stores) - set(team_ids)
        for tid in stale_team_ids:
            self._team_doc_stores.pop(tid, None)

        for tid in team_ids:
            if tid in self._team_doc_stores:
                continue
            self._team_doc_stores[tid] = self._build_team_doc_store(tid, client=client)

        self._sync_workspace_docs()

    def _build_team_doc_store(
        self,
        team_id: str,
        *,
        client=None,
    ) -> "CompositeDocStore":
        """Build a CompositeDocStore for *team_id*, falling back to local-only
        when the team isn't writable by agents."""
        agent_cfg = self.config.agent
        team_info = self.team_registry.get_team(team_id)
        team_slug = team_info.slug if team_info else None
        team_name = team_info.name if team_info else None

        from .memory.team_paths import ensure_team_dir

        team_dir = ensure_team_dir(
            self._workspace,
            team_id,
            team_slug=team_slug,
            team_name=team_name,
        )

        local = LocalDocStore(
            self._workspace,
            agent_name=agent_cfg.name,
            team_id=team_id,
            team_slug=team_slug,
            team_name=team_name,
            rhythm=self.config.memory.rhythm,
        )
        if team_info and not team_info.agent_can_create:
            logger.warning(
                "Team %s is not writable by agents (source_policy=%s); using local team docs",
                team_id,
                team_info.source_policy,
            )
            return CompositeDocStore(local=local, ouro=None)
        ouro = OuroDocStore(
            agent_name=agent_cfg.name,
            org_id=agent_cfg.org_id,
            team_id=team_id,
            client=client if client is not None else self._get_ouro_client(),
            registry_path=team_dir / "state.json",
            team_slug=team_slug,
            team_name=team_name,
            rhythm=self.config.memory.rhythm,
        )
        return CompositeDocStore(local=local, ouro=ouro)

    def doc_store_for(self, team_id: str) -> DocStore:
        """Return the per-team CompositeDocStore, creating it lazily."""
        if team_id in self._team_doc_stores:
            return self._team_doc_stores[team_id]
        if not self.config.agent.org_id:
            return self.doc_store
        store = self._build_team_doc_store(team_id)
        self._team_doc_stores[team_id] = store
        return store

    def _sync_workspace_docs(self) -> None:
        """Bidirectional sync between local team docs and Ouro posts."""
        if not self._team_doc_stores:
            return
        from .memory.workspace_sync import sync_workspace

        ouro_doc_stores: dict[str, OuroDocStore] = {}
        for tid, store in self._team_doc_stores.items():
            inner = store.ouro if isinstance(store, CompositeDocStore) else None
            if inner is not None:
                ouro_doc_stores[tid] = inner
        if not ouro_doc_stores:
            return
        result = sync_workspace(
            workspace=self._workspace,
            team_doc_stores=ouro_doc_stores,
            agent_name=self.config.agent.name,
        )
        if result.pushed:
            logger.info("Workspace sync pushed: %s", ", ".join(result.pushed))
        if result.pulled:
            logger.info("Workspace sync pulled: %s", ", ".join(result.pulled))
        if result.errors:
            for err in result.errors:
                logger.warning("Workspace sync error: %s", err)

    def connect_mcp(self) -> None:
        """Connect to all configured MCP servers once. Safe to call multiple times."""
        if self._mcp_connected:
            return

        for server in self.config.mcp_servers:
            self._connect_one_server(server)
        self._mcp_connected = True

        try:
            self._refresh_platform_context()
        except Exception as e:
            logger.warning("Failed to refresh platform context at startup: %s", e)

    # OpenAI tool schemas reject regex lookaround; Zod email patterns use it.
    _SCHEMA_LOOKAROUND_RE = re.compile(r"\(\?[=!<]")

    @classmethod
    def _strip_unsupported_schema_patterns(cls, node) -> None:
        """Drop ``pattern`` values that use lookaround (unsupported by OpenAI)."""
        if isinstance(node, dict):
            pattern = node.get("pattern")
            if isinstance(pattern, str) and cls._SCHEMA_LOOKAROUND_RE.search(pattern):
                node.pop("pattern", None)
            for value in node.values():
                cls._strip_unsupported_schema_patterns(value)
        elif isinstance(node, list):
            for item in node:
                cls._strip_unsupported_schema_patterns(item)

    @classmethod
    def _patch_tool_inputs(cls, mcp_tool) -> None:
        """Fix mcpadapt's schema conversion for nullable/optional MCP params.

        mcpadapt doesn't translate anyOf: [{type: X}, {type: null}] into
        smolagents' nullable flag, causing validation errors when the LLM
        sends null or omits optional parameters.  It also forces a missing
        top-level ``type`` to ``"string"`` even when ``anyOf`` allows
        multiple types (e.g. string | array), which makes smolagents reject
        valid array arguments.  We collapse non-null ``anyOf`` types into
        ``type`` (string or list) and remove ``anyOf`` so smolagents'
        schema helpers don't crash on entries missing a ``type`` key.

        Also strips regex lookaround from nested ``pattern`` fields (e.g. Zod
        ``z.email()`` via resend-mcp), which OpenAI rejects as invalid_json_schema.
        """
        inputs = getattr(mcp_tool, "inputs", {}) or {}
        cls._strip_unsupported_schema_patterns(inputs)

        for schema in inputs.values():
            any_of = schema.get("anyOf", [])
            if not any_of:
                if "default" in schema:
                    schema["nullable"] = True
                continue

            has_null = any(item.get("type") == "null" for item in any_of)
            non_null_types = [
                item.get("type", "string")
                for item in any_of
                if item.get("type") != "null"
            ]
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_types: list[str] = []
            for t in non_null_types:
                if t not in seen:
                    seen.add(t)
                    unique_types.append(t)

            if not unique_types:
                schema.setdefault("type", "string")
            elif len(unique_types) == 1:
                schema["type"] = unique_types[0]
            else:
                schema["type"] = unique_types

            if has_null or "default" in schema:
                schema["nullable"] = True
            del schema["anyOf"]

    def _connect_one_server(self, server: MCPServerConfig) -> None:
        if server.transport == "stdio":
            if not server.command:
                return
            try:
                from mcp import StdioServerParameters

                env = self._mcp_server_env(server)
                server_params = StdioServerParameters(
                    command=server.command, args=server.args or [], env=env
                )
                ctx = ToolCollection.from_mcp(
                    server_parameters=server_params,
                    trust_remote_code=True,
                    structured_output=False,
                )
                collection = ctx.__enter__()
                self._mcp_contexts.append(ctx)
                self._mcp_locks.register_stdio(server.name)
                self._register_mcp_tools(server, collection.tools, lock_stdio=True)
                logger.info("Connected to MCP server: %s (stdio)", server.name)
            except Exception as e:
                logger.error("Failed to connect to MCP server %s: %s", server.name, e)
        elif server.transport == "streamable-http":
            if not server.url:
                logger.error(
                    "MCP server %s: streamable-http requires url", server.name
                )
                return
            try:
                if server.command:
                    managed = spawn_managed_mcp_http(
                        name=server.name,
                        command=server.command,
                        args=server.args,
                        env=self._mcp_server_env(server),
                        url=server.url,
                    )
                    self._managed_mcp.append(managed)
                params = {"url": server.url, "transport": "streamable-http"}
                ctx = ToolCollection.from_mcp(
                    server_parameters=params,
                    trust_remote_code=True,
                    structured_output=False,
                )
                collection = ctx.__enter__()
                self._mcp_contexts.append(ctx)
                self._mcp_locks.register_http(server.name)
                self._register_mcp_tools(server, collection.tools, lock_stdio=False)
                logger.info(
                    "Connected to MCP server: %s (streamable-http %s)",
                    server.name,
                    server.url,
                )
            except Exception as e:
                logger.error("Failed to connect to MCP server %s: %s", server.name, e)

    def _mcp_server_env(self, server: MCPServerConfig) -> dict[str, str]:
        env = dict(server.env or {})
        env.setdefault("WORKSPACE_ROOT", str(self._workspace.resolve()))
        if self.config.agent.sandbox.mode == "docker":
            env.setdefault(
                "WORKSPACE_MOUNT",
                self.config.agent.sandbox.workspace_mount,
            )
        if server.name == "ouro":
            agent_tz = (
                (self.config.heartbeat.active_hours or {}).get("timezone")
                if self.config.heartbeat.active_hours
                else None
            )
            if agent_tz:
                env.setdefault("OURO_MCP_TIMEZONE", agent_tz)
        return env

    def _register_mcp_tools(
        self,
        server: MCPServerConfig,
        tools,
        *,
        lock_stdio: bool,
    ) -> None:
        if server.description:
            self._server_descriptions[server.name] = server.description
        for mcp_tool in tools:
            self._patch_tool_inputs(mcp_tool)
            if lock_stdio:
                wrap_mcp_tool_with_lock(
                    mcp_tool, server_name=server.name, locks=self._mcp_locks
                )
            qualified_name = f"{server.name}:{mcp_tool.name}"
            self._deferred_tools[qualified_name] = mcp_tool
            self._deferred_index.append(
                {
                    "tool": qualified_name,
                    "server": server.name,
                    "raw_name": mcp_tool.name,
                    "description": " ".join(
                        (mcp_tool.description or "").strip().split()
                    ),
                    "inputs": getattr(mcp_tool, "inputs", {}),
                    "output_type": getattr(mcp_tool, "output_type", "string"),
                }
            )
            self._deferred_tools_by_raw_name.setdefault(mcp_tool.name, []).append(
                qualified_name
            )

    def cancel_active_runs(self, reason: str = "shutdown") -> None:
        """Ask all in-flight smolagents loops owned by this agent to stop."""
        with self._active_runs_lock:
            tokens = list(self._active_run_tokens)
        for token in tokens:
            token.cancel(reason)
        for token in self._active_runs.all_tokens():
            token.cancel(reason)

    def close(self) -> None:
        """Shut down all MCP server connections."""
        self.cancel_active_runs("agent closing")
        for managed in self._managed_mcp:
            try:
                managed.stop()
            except Exception:
                pass
        self._managed_mcp.clear()
        for ctx in self._mcp_contexts:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        self._mcp_contexts.clear()
        self._mcp_locks.clear()
        self._deferred_tools.clear()
        self._deferred_tools_by_raw_name.clear()
        self._deferred_index.clear()
        self._server_descriptions.clear()
        self._mcp_connected = False

    def __enter__(self):
        self.connect_mcp()
        return self

    def __exit__(self, *exc):
        self.close()

    async def __aenter__(self):
        self.connect_mcp()
        return self

    async def __aexit__(self, *exc):
        self.close()

    def _resolve_tool_name(self, tool_name: str) -> tuple[Optional[str], Optional[str]]:
        if tool_name in self._deferred_tools:
            return tool_name, None
        candidates = self._deferred_tools_by_raw_name.get(tool_name, [])
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            return (
                None,
                f"Ambiguous tool name '{tool_name}'. Use one of: {', '.join(candidates)}",
            )
        return None, f"Unknown tool '{tool_name}'."

    def _filter_deferred_for_profile(
        self,
        profile: ModeProfile,
        allowed_servers: Optional[list[str]] = None,
    ) -> tuple[dict, list[dict]]:
        """Narrow the deferred MCP tool set to what a mode profile may use."""
        deferred_tools = self._deferred_tools
        deferred_index = self._deferred_index

        if profile.restricted_servers:
            servers = (
                set(allowed_servers)
                if allowed_servers
                else set(profile.default_servers)
            )
            deferred_tools, deferred_index = filter_deferred_by_servers(
                deferred_tools, deferred_index, servers
            )

        if profile.excluded_tools:
            deferred_tools, deferred_index = filter_deferred_excluding(
                deferred_tools, deferred_index, profile.excluded_tools
            )

        if profile.allowed_capabilities is not None:
            deferred_tools, deferred_index = filter_deferred_tools(
                deferred_tools,
                deferred_index,
                profile.allowed_capabilities,
            )

        return deferred_tools, deferred_index

    def _build_agent_tools(
        self,
        profile: ModeProfile,
        user_id: Optional[str] = None,
        allowed_servers: Optional[list[str]] = None,
        preload_tools: Optional[list[str]] = None,
        conversation_id: Optional[str] = None,
        run_id: str = "",
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        run_mode: str = "",
        event_type: str = "",
        cancellation_token: Optional[RunCancellationToken] = None,
        observer: Optional[AgentObserver] = None,
    ):
        """Build the tool list and directory string for a single run.

        Returns (all_tools, deferred_tool_directory, agent_ref, preloaded_names).
        ``preloaded_names`` lists the raw call names of tools that were eagerly
        resolved and added to ``all_tools`` so the agent can use them without
        calling ``load_tool`` first.
        """
        deferred_tools, deferred_index = self._filter_deferred_for_profile(
            profile, allowed_servers
        )

        agent_self = self
        agent_ref: dict = {}

        from .tools.mcp_tools import (
            _resolve_tool_name,
            format_deferred_directory,
            make_load_tool,
        )

        load_tool = (
            make_load_tool(
                deferred_tools,
                deferred_index,
                agent_ref,
                server_descriptions=self._server_descriptions,
            )
            if profile.allows_capability(Capability.LOAD_MCP_TOOL)
            else None
        )

        active_doc_store = self._resolve_doc_store(team_id=team_id, doc_store=doc_store)
        memory_tools = make_memory_tools(
            self.memory,
            self.config.agent.name,
            user_id=user_id,
            workspace=self.config.agent.workspace,
            doc_store=active_doc_store,
            team_id=team_id,
            memory_categories=getattr(profile, "memory_scopes", []) or [],
            conversation_id=conversation_id,
            run_id=run_id,
            mode=run_mode,
            event_type=event_type,
            org_id=self.config.agent.org_id or "",
            available_team_ids=self._available_memory_team_ids(team_id),
            available_teams=self._available_memory_teams(team_id),
            enable_remember=profile.allows_capability(Capability.MEMORY_WRITE),
            search_limit=self.config.memory.search_limit,
            max_retrieval_tokens=self.config.memory.max_retrieval_tokens,
            min_signal_score=self.config.memory.min_signal_score,
        )
        if profile.memory_tool_filter is not None:
            allowed = set(profile.memory_tool_filter)
            memory_tools = [t for t in memory_tools if t.name in allowed]
        elif not profile.allows_capability(Capability.MEMORY_WRITE):
            memory_tools = [t for t in memory_tools if t.name != "remember"]

        # Heartbeat and chat get path-confined read_context so indexes /
        # recent-activity digests are actionable (period logs, tasks, memory).
        if profile.name in ("heartbeat", "chat"):
            from .memory.context_loader import make_read_context_tool

            memory_tools = list(memory_tools) + [
                make_read_context_tool(
                    self.config.agent.workspace,
                    doc_store=active_doc_store,
                    agent_name=self.config.agent.name,
                )
            ]

        code_tools, python_executor = make_code_tools(
            workspace=self.config.agent.workspace,
            ouro_client=self._get_ouro_client(),
            python_packages=self.config.agent.sandbox.python_packages or None,
            package_versions=self._python_package_versions or None,
            sandbox_config=self.config.agent.sandbox,
            agent_name=self.config.agent.name,
            run_id=run_id,
        )
        if profile.allowed_capabilities is not None:
            allowed_code_names: set[str] = set()
            if profile.allows_capability(Capability.RUN_PYTHON):
                allowed_code_names.add("run_python")
            if profile.allows_capability(Capability.RUN_SHELL):
                allowed_code_names.add("run_shell")
            code_tools = [tool for tool in code_tools if tool.name in allowed_code_names]
        closeables = [python_executor] if hasattr(python_executor, "close") else []

        agent_route_tools: list = []
        if (
            self.config.agent_routes.enabled
            and profile.allows_capability(Capability.RUN_PYTHON)
            and hasattr(python_executor, "execute")
            and self.config.agent.sandbox.mode == "docker"
        ):
            agent_route_tools.append(
                make_run_coil_tool(self.config.agent.workspace, python_executor)
            )
            agent_route_tools.extend(
                make_publish_route_tools(
                    self.config.agent.workspace,
                    routes_config=self.config.agent_routes,
                    agent_name=self.config.agent.name,
                    ouro_client=self._get_ouro_client(),
                    allow_publish=profile.allows_capability(Capability.CREATE_ASSET),
                    public_base_url=self.config.server.public_base_url,
                )
            )
        elif self.config.agent_routes.enabled and self.config.agent.sandbox.mode != "docker":
            logger.info(
                "agent_routes enabled but sandbox.mode=%s; skipping run_coil "
                "(coil handlers require Docker)",
                self.config.agent.sandbox.mode,
            )

        load_skill = (
            make_load_skill_tool(self.config.agent.workspace)
            if profile.allows_capability(Capability.LOAD_MCP_TOOL)
            else None
        )
        scheduler_tools = (
            make_scheduler_tools(self.scheduler, team_id=team_id)
            if not profile.restricted_servers
            and profile.allows_capability(Capability.SCHEDULE)
            else []
        )
        run_history_tools = (
            make_run_history_tools(
                self._run_log,
                current_run_id=self._active_run_id(),
                team_id=team_id,
                conversation_id=conversation_id,
                default_scope=self.config.run_log.agent_default_scope,
                max_results=self.config.run_log.agent_max_results,
                max_detail_chars=self.config.run_log.agent_max_detail_chars,
            )
            if self.config.run_log.enabled and self.config.run_log.expose_to_agent
            else []
        )
        controller_tools = (
            [
                self._controller_questions.make_tool(
                    cancellation_token=cancellation_token,
                    current_conversation_id=conversation_id,
                    current_user_id=user_id,
                )
            ]
            if self.config.ask_controller.enabled
            and self.config.security.resolved_controller_ids
            and profile.allows_capability(Capability.SEND_MESSAGE)
            else []
        )

        # Build the delegate tool for subagent dispatch
        delegatable_profiles = agent_self.delegatable_profiles
        subagent_names = list(delegatable_profiles.keys())
        parent_allowed_capabilities = profile.allowed_capabilities

        _subagent_names_str = ", ".join(subagent_names)

        def _do_delegate(
            subagent: str,
            task: str,
            asset_refs: Optional[list[str]] = None,
        ) -> tuple:
            """Shared delegation logic. Returns (result, profile) or (None, None)."""
            profile = delegatable_profiles.get(subagent)
            if not profile:
                return None, None

            result = agent_self._run_subagent(
                profile,
                task,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
                asset_refs=asset_refs or [],
                team_id=team_id,
                doc_store=active_doc_store,
                cancellation_token=cancellation_token,
                progress_observer=observer,
                allowed_capabilities=parent_allowed_capabilities,
            )
            return result, profile

        def _format_delegate_result(
            result,
            profile,
            subagent: str,
            return_mode: str = "",
        ) -> dict:
            """Format a subagent result for return to the main agent."""
            mode = normalize_return_mode(
                return_mode,
                getattr(profile, "default_return_mode", "summary_only"),
            )
            error_payload = validate_delegate_result(
                result,
                subagent,
                mode,
                available=subagent_names,
            )
            if error_payload:
                return error_payload

            assert result is not None
            assert profile is not None

            mode = resolve_auto_return_mode(
                mode,
                has_asset=bool(result.asset_id),
            )

            summary = result.asset_description or summarize_delegate_text(result.text)
            return delegate_success_payload(result, subagent, mode, summary)

        @tool
        def delegate(tasks: list[dict]) -> str:
            """Delegate one or more tasks to specialized subagents (multiple run in parallel). See the SUBAGENTS section in the system prompt for when to delegate. Subagents publish their own asset and return its `asset_id`/`link` — surface that link; do NOT republish or paste the full body.

            Args:
                tasks: List of task specs. Each is a dict with keys:
                    - subagent (str, required): Name of the subagent (see subagent directory in system prompt).
                    - task (str, required): A clear, self-contained description of what the subagent should do.
                    - asset_refs (list[str], optional): Ouro asset UUIDs to pass as input context.
                    - return_mode (str, optional): summary_only, full_text, or auto. Defaults to the subagent profile setting.

            Example: [{"subagent": "research", "task": "Find papers on X"}, {"subagent": "writer", "task": "Draft intro"}]
            """
            if not tasks:
                return json.dumps({"status": "error", "error": "No tasks provided."})

            def _run_one(spec: dict) -> dict:
                sa = spec.get("subagent", "")
                task_str = spec.get("task", "")
                refs = spec.get("asset_refs")
                logger.info("Delegating to subagent '%s': %s", sa, task_str[:120])
                result, profile = _do_delegate(sa, task_str, refs)
                return _format_delegate_result(
                    result,
                    profile,
                    sa,
                    spec.get("return_mode", ""),
                )

            outputs = dispatch_delegate_tasks(
                tasks,
                _run_one,
                parallel=bool(agent_self.config.subagents.parallel_dispatch),
                max_workers=4,
            )
            return dumps_delegate_result(tasks, outputs)

        delegate.description += f"\n\nAvailable subagents: {_subagent_names_str}"

        delegate_tools = (
            []
            if (
                not self.config.subagents.enabled
                or not profile.allow_delegation
                or not profile.allows_capability(Capability.DELEGATE)
            )
            else [delegate]
        )

        _has_memory_filter = profile.memory_tool_filter is not None
        if _has_memory_filter:
            # Filtered modes (plan / review) keep a narrow memory surface, but
            # self-recall is read-only and valuable for planning over past
            # cycles, so it's exposed here too.
            all_tools = list(memory_tools) + run_history_tools + controller_tools
        else:
            all_tools = (
                list(memory_tools)
                + scheduler_tools
                + run_history_tools
                + controller_tools
                + delegate_tools
                + [tool for tool in (load_tool, load_skill) if tool is not None]
                + code_tools
                + agent_route_tools
            )

        preloaded_names: list[str] = []
        for qualified_name in preload_tools or []:
            resolved, err = _resolve_tool_name(
                qualified_name,
                deferred_tools,
                deferred_index,
            )
            if err or not resolved:
                logger.warning("Preload skipped for '%s': %s", qualified_name, err)
                continue
            target = deferred_tools.get(resolved)
            if not target:
                logger.warning(
                    "Preload skipped for '%s': not in available deferred tools",
                    resolved,
                )
                continue
            item = next((i for i in deferred_index if i["tool"] == resolved), None)
            if item:
                all_tools.append(target)
                preloaded_names.append(item["raw_name"])
                logger.info(
                    "Preloaded tool: %s (call as %s)", resolved, item["raw_name"]
                )

        if _has_memory_filter or load_tool is None:
            deferred_tool_directory = ""
        else:
            deferred_tool_directory = format_deferred_directory(
                deferred_index,
                set(profile.default_servers),
                self._server_descriptions,
            )

        return (
            all_tools,
            deferred_tool_directory,
            agent_ref,
            preloaded_names,
            closeables,
        )

    def _build_system_prompt(
        self,
        task: str,
        profile: ModeProfile,
        conversation_id: Optional[str],
        deferred_tool_directory: str,
        user_id: Optional[str] = None,
        mode_framing_override: str = "",
        preloaded_tool_names: Optional[list[str]] = None,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        trigger_turn_id: Optional[str] = None,
        include_plans_index: bool = True,
        entity_haystack: str = "",
    ) -> tuple[str, str]:
        """Build the system prompt and dynamic context.

        Returns (system_prompt, dynamic_context) where dynamic_context should
        be prepended to the task message for prompt-cache-friendly layout.

        ``include_plans_index`` is False for quest_work heartbeats (the inbox
        already is the actionable plan surface).
        """
        conversation_context = ""
        # Conversational modes inject history as structured memory steps;
        # non-chat modes with a conversation_id get a text summary instead.
        if (
            conversation_id
            and not profile.lightweight
            and not profile.conversational
        ):
            turns = self._resolve_conversation_turns(
                conversation_id,
                limit=24,
                trigger_turn_id=trigger_turn_id,
            )
            conversation_context = format_conversation_turns(
                turns, summarize_fn=self._summarize_turns
            )

        skills_text = self.skills
        # Heartbeat is lightweight but still has load_skill available; keep a
        # compact directory so skill use is discoverable without inlining bodies.
        skill_directory = (
            self.skill_directory
            if (not profile.lightweight or profile.memory_tool_filter is None)
            and profile.allows_capability(Capability.LOAD_MCP_TOOL)
            else ""
        )

        scope_team_id = memory_team_id(team_id)
        active_doc_store = self._resolve_doc_store(
            team_id=scope_team_id, doc_store=doc_store
        )
        shared_context = self._load_shared_prompt_context(
            user_id=user_id,
            include_scheduled_tasks=profile.load_scheduled_tasks,
            team_id=team_id,
            doc_store=active_doc_store,
        )
        working_memory = shared_context["working_memory"]
        if profile.conversational:
            activity = self._cross_team_recent_activity_digest()
            if activity:
                working_memory = (
                    f"{working_memory}\n\n{activity}" if working_memory else activity
                )
        user_model_text = shared_context["user_model"]

        from .memory.context_loader import load_entity_context

        entity_context_text = load_entity_context(
            self.config.agent.workspace,
            haystack=entity_haystack or task,
            task=task,
            doc_store=active_doc_store,
            agent_name=self.config.agent.name,
            team_id=scope_team_id,
        )
        plans_index_text = ""
        if include_plans_index:
            plans_index_text = self._own_quests_index(
                pointer=bool(profile.conversational)
            )

        delegatable_profiles = self.delegatable_profiles
        # Heartbeat is lightweight but still needs a compact directory so it
        # knows when to delegate search/research. Decouple ``lightweight``
        # from hiding the subagent directory.
        show_subagents = (
            profile.allow_delegation
            and bool(delegatable_profiles)
            and self.config.subagents.enabled
            and (
                not profile.lightweight
                or profile.name == "heartbeat"
            )
        )
        if show_subagents:
            if profile.name == "heartbeat":
                heartbeat_names = (
                    "search",
                    "research",
                    "writer",
                    "executor",
                    "developer",
                )
                listed = [
                    p
                    for name in heartbeat_names
                    if (p := delegatable_profiles.get(name)) is not None
                ]
            else:
                listed = list(delegatable_profiles.values())
            subagent_directory = "\n".join(
                f"- **{p.name}**: {p.description}" for p in listed
            )
        else:
            subagent_directory = ""

        framing = mode_framing_override or profile.framing
        if (
            profile.conversational
            and user_id
            and user_id in (self.config.security.resolved_controller_ids or [])
        ):
            from .modes.framing import CHAT_CONTROLLER_STEERING

            framing = f"{framing}\n\n{CHAT_CONTROLLER_STEERING}"
        if (
            self.config.ask_controller.enabled
            and self.config.security.resolved_controller_ids
            and profile.allows_capability(Capability.SEND_MESSAGE)
        ):
            framing = f"{framing}\n\n{ASK_CONTROLLER_GUIDANCE}"

        coil_directory = ""
        if (
            self.config.agent_routes.enabled
            and self.config.agent.sandbox.mode == "docker"
            and profile.allows_capability(Capability.RUN_PYTHON)
            and not profile.lightweight
        ):
            from .tools.agent_route_tools import build_coil_directory

            coil_directory = build_coil_directory(self.config.agent.workspace)

        return build_prompt(
            soul=shared_context["soul"],
            notes=shared_context["notes"],
            skills=skills_text,
            profile=profile,
            skill_directory=skill_directory,
            working_memory=working_memory,
            conversation_context=conversation_context,
            user_model=user_model_text,
            entity_context=entity_context_text,
            deferred_tool_directory=deferred_tool_directory,
            subagent_directory=subagent_directory,
            coil_directory=coil_directory,
            mode_framing_override=framing,
            platform_context=shared_context["platform_context"],
            chat_conversation_id=(
                conversation_id if profile.include_chat_conversation_id else None
            ),
            preloaded_tool_names=preloaded_tool_names,
            plans_index=plans_index_text,
            workspace_root=self.config.agent.sandbox.agent_facing_root(
                self._workspace
            ),
        )

    def resolve_controller_reply(
        self,
        *,
        conversation_id: str,
        controller_user_id: str,
        text: str,
        message_id: Optional[str] = None,
    ) -> ControllerReplyResolution:
        """Resolve a controller DM before it starts a competing chat run."""
        return self._controller_questions.resolve_reply(
            conversation_id=conversation_id,
            controller_user_id=controller_user_id,
            text=text,
            message_id=message_id,
        )

    def _resolve_subagent_model(
        self,
        profile,
        *,
        usage_tracker: Optional[UsageTracker] = None,
    ) -> "TrackedOpenAIModel":
        """Resolve the model for a subagent profile using the override cascade."""
        override = self.config.subagents.profiles.get(profile.name)
        model_id = (
            profile.model_override
            or (override.model if override else None)
            or self.config.subagents.default_model
            or self._model_id_for_role(profile.name)
            or self.config.agent.model
        )
        role = profile.name
        # During heartbeat ticks, delegated workers stay at mid/light even if
        # the profile defaults to strong in chat/autonomous modes. The main
        # heartbeat runs at mid (strong when no mid tier is configured); this
        # clamp only applies to subagents.
        if self._get_heartbeat_cheap_workers():
            cheap_id = None
            tiers = self.config.models
            if tiers is not None:
                mid_spec = tiers.mid or tiers.light
                if mid_spec is not None:
                    cheap_id = mid_spec.id
            if not cheap_id:
                cheap_id = (
                    self._model_id_for_role("utility")
                    or self.config.heartbeat.model
                )
            strong_id = self._model_id_for_role("agent") or self.config.agent.model
            if cheap_id and model_id == strong_id:
                model_id = cheap_id
                role = "utility" if not (tiers and tiers.mid) else "chat"
        return self._build_model(
            model_id,
            subagent_profile=profile.name,
            role=role,
            usage_tracker=usage_tracker,
        )

    def _apply_profile_overrides(self, profile):
        """Apply config overrides (max_steps, etc.) to a profile."""
        override = self.config.subagents.profiles.get(profile.name)
        if override and override.max_steps is not None:
            return profile.model_copy(update={"max_steps": override.max_steps})
        return profile

    def _build_subagent_context(
        self,
        profile,
        model,
        task: str = "",
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        asset_refs: Optional[list[str]] = None,
        usage_tracker: Optional[UsageTracker] = None,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        cancellation_token: Optional[RunCancellationToken] = None,
        progress_observer: Optional[AgentObserver] = None,
        allowed_capabilities: Optional[frozenset[Capability]] = None,
    ) -> "SubAgentContext":
        from .subagents.context import SubAgentContext
        from .tools.observation_policy import to_observation_policy

        ouro_client = (
            self._get_ouro_client()
            if getattr(profile, "needs_python_tool", False)
            else None
        )

        active_doc_store = self._resolve_doc_store(team_id=team_id, doc_store=doc_store)
        shared_context = self._load_shared_prompt_context(
            user_id=user_id,
            include_scheduled_tasks=False,
            team_id=team_id,
            doc_store=active_doc_store,
        )

        return SubAgentContext(
            workspace=self._workspace,
            backend=self.memory,
            agent_id=self.config.agent.name,
            memory_config=self.config.memory,
            model=model,
            observation_policy=to_observation_policy(self.config.observations),
            user_id=user_id,
            conversation_id=conversation_id,
            deferred_tools=self._deferred_tools,
            deferred_index=self._deferred_index,
            server_descriptions=self._server_descriptions,
            run_id=run_id,
            soul=shared_context["soul"],
            notes=shared_context["notes"],
            platform_context=shared_context["platform_context"],
            working_memory=shared_context["working_memory"],
            user_model=shared_context["user_model"],
            plans_index=shared_context["plans_index"],
            doc_store=active_doc_store,
            team_id=team_id,
            asset_refs=list(asset_refs or []),
            memory_scopes=getattr(profile, "memory_scopes", []) or [],
            ouro_client=ouro_client,
            python_packages=self.config.agent.sandbox.python_packages or [],
            python_package_versions=self._python_package_versions or {},
            sandbox_config=self.config.agent.sandbox,
            record_subagent_usage=self._record_subagent_usage,
            record_subagent_run=(
                self._record_subagent_run
                if (
                    self.config.run_log.enabled
                    and self.config.run_log.capture_subagent_runs
                )
                else None
            ),
            cancellation_token=cancellation_token,
            progress_observer=progress_observer,
            delegatable_profiles=self.delegatable_profiles,
            allowed_capabilities=allowed_capabilities,
        )

    def _record_subagent_usage(self, name: str, usage: SubAgentUsage) -> None:
        self._active_subagent_ledger().append((name, usage))

    def _record_subagent_run(
        self,
        *,
        name: str,
        run_id: str,
        task: str,
        result: str,
        status: str,
        error: Optional[str],
        usage: Optional[SubAgentUsage],
        agent,
        started_at: str,
        duration_s: Optional[float],
    ) -> None:
        """Write a subagent run to the run log as a child of the current run."""
        try:
            record = RunRecord(
                run_id=run_id,
                agent_name=self.config.agent.name,
                mode=f"subagent:{name}",
                status=status,
                parent_run_id=self._active_run_id(),
                tick_id=(
                    (get_run_context().tick_id if get_run_context() else None)
                    or self._current_tick_id
                ),
                started_at=started_at,
                task=task or "",
                model=getattr(usage, "model_id", "") or "",
            )
            record.result = "" if result is None else str(result)
            if error:
                record.error_message = error
            record.finalize_timing(duration_s)
            if usage is not None:
                record.input_tokens = usage.input_tokens
                record.output_tokens = usage.output_tokens
                record.cached_input_tokens = usage.cached_input_tokens
                record.reasoning_tokens = usage.reasoning_tokens
                record.total_tokens = usage.total_tokens
                record.num_api_calls = usage.llm_calls
                record.cost_usd = usage.cost_usd
                try:
                    record.usage_json = json.dumps(usage.to_dict(), default=str)
                except Exception:
                    pass
            cfg = self.config.run_log
            if cfg.capture_steps and agent is not None:
                cap = cfg.max_observation_chars if cfg.capture_observations else 0
                steps = extract_run_steps(agent, max_observation_chars=cap)
                if not cfg.capture_observations:
                    for step in steps:
                        step.observations = None
                if not cfg.capture_reasoning:
                    for step in steps:
                        step.reasoning = None
                record.set_steps(steps)
            self._run_log.write(record)
        except Exception:
            logger.warning("Failed to record subagent run", exc_info=True)

    def _run_subagent(
        self,
        profile,
        task: str,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        asset_refs: Optional[list[str]] = None,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        cancellation_token: Optional[RunCancellationToken] = None,
        progress_observer: Optional[AgentObserver] = None,
        allowed_capabilities: Optional[frozenset[Capability]] = None,
        model_override=None,
    ):
        """Build context and dispatch a subagent through the unified runner.

        Returns a SubAgentResult with .text, .success, .error, and .usage fields.
        """
        from .subagents.runner import run_subagent

        effective_profile = self._apply_profile_overrides(profile)
        subagent_usage_tracker = MirroredUsageTracker(
            UsageTracker(),
            mirrors=[self._active_usage_tracker()],
        )
        model = model_override or self._resolve_subagent_model(
            profile,
            usage_tracker=subagent_usage_tracker,
        )

        ctx = self._build_subagent_context(
            effective_profile,
            model,
            task=task,
            conversation_id=conversation_id,
            user_id=user_id,
            run_id=run_id,
            asset_refs=asset_refs,
            usage_tracker=subagent_usage_tracker,
            team_id=team_id,
            doc_store=doc_store,
            cancellation_token=cancellation_token,
            progress_observer=progress_observer,
            allowed_capabilities=allowed_capabilities,
        )

        return run_subagent(effective_profile, task, ctx)

    def _run_subagents_parallel(
        self,
        tasks: list[tuple],
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        cancellation_token: Optional[RunCancellationToken] = None,
        progress_observer: Optional[AgentObserver] = None,
        allowed_capabilities: Optional[frozenset[Capability]] = None,
    ) -> list:
        """Run multiple subagents in parallel.

        Each task is a tuple of (profile, task_str) or
        (profile, task_str, extra_kwargs_dict).
        Returns results in input order.
        """
        from .subagents.runner import run_subagents_parallel

        dispatch_list = []
        for item in tasks:
            if len(item) == 2:
                profile, task_str = item
                extra = {}
            else:
                profile, task_str, extra = item

            effective_profile = self._apply_profile_overrides(profile)
            subagent_usage_tracker = MirroredUsageTracker(
                UsageTracker(),
                mirrors=[self._active_usage_tracker()],
            )
            model = self._resolve_subagent_model(
                profile,
                usage_tracker=subagent_usage_tracker,
            )

            ctx = self._build_subagent_context(
                effective_profile,
                model,
                task=task_str,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
                asset_refs=extra.get("asset_refs"),
                usage_tracker=subagent_usage_tracker,
                team_id=team_id,
                doc_store=doc_store,
                cancellation_token=cancellation_token,
                progress_observer=progress_observer,
                allowed_capabilities=allowed_capabilities,
            )
            dispatch_list.append((effective_profile, task_str, ctx))

        return run_subagents_parallel(dispatch_list)

    def _build_step_callback(
        self,
        status_callback: Optional[RunStatusCallback],
        display: Optional[OuroDisplay] = None,
        progress_callback: Optional[Callable[[ProgressEvent], None]] = None,
    ) -> Callable[[ActionStep], None]:
        return build_step_callback(
            self._active_usage_tracker(),
            status_callback=status_callback,
            display=display,
            progress_callback=progress_callback,
        )

    def _run_reflection(
        self,
        task: str,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        display: Optional[OuroDisplay] = None,
        status_callback: Optional[RunStatusCallback] = None,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        observer: Optional[AgentObserver] = None,
    ) -> Optional[ReflectionResult]:
        """Run the reflector subagent as a visible step.

        Shows a display step, tracks usage via the subagent ledger, and
        returns a parsed ``ReflectionResult`` (or None on failure).
        """
        from .subagents.profiles import REFLECTOR

        if "Available teams (use only these IDs in team_ids)" not in task:
            task = f"{self._format_available_memory_teams(team_id)}\n\n{task}"

        _display = display or get_display()
        _display.step("reflecting...")
        emit_progress(observer, "reflecting", "post-run reflection")
        if status_callback:
            try:
                status_callback("thinking", "Reflecting...", True)
            except Exception:
                logger.exception("Failed to emit reflection status")

        t0 = time.monotonic()
        result = self._run_subagent(
            REFLECTOR,
            task,
            conversation_id=conversation_id,
            user_id=user_id,
            run_id=run_id,
            team_id=team_id,
            doc_store=doc_store,
            progress_observer=observer,
        )
        duration_s = time.monotonic() - t0

        if result.usage and result.usage.total_tokens:
            _display.token_summary(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                current_context_tokens=result.usage.current_context_tokens,
                duration_s=duration_s,
                cost_usd=result.usage.cost_usd,
            )

        if not result.success:
            logger.warning("Reflector subagent failed: %s", result.error)
            emit_progress(
                observer, "reflecting", result.error or "failed", state="failed"
            )
            return None

        emit_progress(observer, "reflecting", "reflection complete", state="complete")
        return parse_reflection_result(result.text)

    def _available_memory_teams(self, team_id: Optional[str] = None) -> list[dict]:
        teams = [
            {"id": team.id, "slug": team.slug, "name": team.name}
            for team in self.team_registry.list_teams()
        ]
        if team_id and not any(team["id"] == team_id for team in teams):
            teams.append({"id": team_id, "slug": "", "name": team_id})
        return teams

    def _available_memory_team_ids(self, team_id: Optional[str] = None) -> set[str]:
        ids = {team["id"] for team in self._available_memory_teams(team_id)}
        if team_id:
            ids.add(team_id)
        return ids

    def _format_available_memory_teams(self, team_id: Optional[str] = None) -> str:
        lines = ["Available teams (use only these IDs in team_ids):"]
        teams = self._available_memory_teams(team_id)
        if not teams:
            lines.append("- (none; leave team_ids = [])")
        else:
            for team in teams:
                lines.append(
                    f"- {team['id']} · {team.get('slug', '')} · {team.get('name', '')}"
                )
        return "\n".join(lines)

    def _post_run_reflect(
        self,
        task: str,
        result: str,
        tool_summary: list[dict],
        mode: RunMode = RunMode.AUTONOMOUS,
        user_id: Optional[str] = None,
        run_id: str = "",
        event_type: Optional[str] = None,
        team_id: Optional[str] = None,
        doc_store: Optional[DocStore] = None,
        conversation_id: Optional[str] = None,
        memory_notes: Optional[list[str]] = None,
        *,
        store_semantic: bool = True,
    ) -> None:
        """Run reflection after an autonomous/event run via the reflector subagent.

        Extracts curated facts (with memory semantics and asset refs) and
        writes a daily log entry. Runs as a proper subagent so usage is tracked
        and the step is visible in the display.

        When *store_semantic* is False (heartbeat ticks with
        ``worth_remembering=false``), still write daily-log episodes but skip
        vector-memory candidates.
        """
        reflection_task = build_run_reflection_task(
            task=task,
            result=str(result),
            tool_summary=tool_summary,
            run_mode=mode.value,
            event_type=event_type,
            available_teams=self._available_memory_teams(team_id),
            memory_notes=memory_notes if store_semantic else None,
            episode_only=not store_semantic,
        )

        active_doc_store = self._resolve_doc_store(team_id=team_id, doc_store=doc_store)
        try:
            reflection = self._run_reflection(
                reflection_task,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
                team_id=team_id,
                doc_store=active_doc_store,
            )
            if not reflection:
                return

            if store_semantic:
                store_reflection_memories(
                    reflection,
                    self.memory,
                    agent_id=self.config.agent.name,
                    user_id=user_id,
                    run_id=run_id,
                    conversation_id="",
                    team_id=team_id,
                    available_team_ids=self._available_memory_team_ids(team_id),
                    org_id=self.config.agent.org_id or "",
                    mode=mode.value,
                    event_type=event_type or "",
                    source=f"run-reflection:{run_id}",
                )

                if reflection.user_preferences and user_id:
                    from .memory.user_model import append_to_user_model

                    try:
                        append_to_user_model(
                            self.config.agent.workspace,
                            user_id,
                            "Preferences",
                            reflection.user_preferences,
                            doc_store=active_doc_store,
                        )
                    except Exception as e:
                        logger.warning("Failed to update user model: %s", e)

            daily_writes = 0
            seen_daily_entries: set[tuple[str, str]] = set()
            for target_team_id, entry_text in validated_daily_log_entries(
                reflection,
                run_team_id=team_id,
                available_team_ids=self._available_memory_team_ids(team_id),
            ):
                normalized_entry = normalize_daily_log_entry(
                    entry_text,
                    mode.value,
                    event_type=event_type,
                )
                dedupe_key = (target_team_id, normalized_entry)
                if dedupe_key in seen_daily_entries:
                    continue
                seen_daily_entries.add(dedupe_key)
                write_log(
                    self.config.agent.workspace,
                    normalized_entry,
                    doc_store=self.doc_store_for(target_team_id),
                    agent_name=self.config.agent.name,
                )
                daily_writes += 1

            logger.info(
                "Post-run reflection: semantic=%s facts=%d daily=%s",
                store_semantic,
                len(reflection.facts_to_store) if store_semantic else 0,
                bool(daily_writes),
            )
        except Exception as e:
            logger.warning("Post-run reflection failed: %s", e)

    async def run(
        self,
        task: str,
        model_override=None,
        conversation_id: Optional[str] = None,
        mode: RunMode = RunMode.AUTONOMOUS,
        user_id: Optional[str] = None,
        skip_memory: bool = False,
        allowed_servers: Optional[list[str]] = None,
        mode_framing_override: str = "",
        preload_tools: Optional[list[str]] = None,
        prefetch: Optional[PrefetchSpec] = None,
        debug_markdown_path: Optional[Path] = None,
        extra_tools: Optional[list] = None,
        observer: Optional[AgentObserver] = None,
        preserve_existing_usage: bool = False,
        event_type: Optional[str] = None,
        team_id: Optional[str] = None,
        capability_envelope: Optional[CapabilityEnvelope] = None,
        cancellation_token: Optional[RunCancellationToken] = None,
        trigger_turn_id: Optional[str] = None,
        preemptible: Optional[bool] = None,
        heartbeat_tick_kind: Optional[str] = None,
        include_plans_index: bool = True,
    ) -> str:
        # ``preemptible`` is retained for API compatibility but ignored —
        # modes overlap; only conversation-scoped interrupt cancels a run.
        del preemptible
        token = cancellation_token or RunCancellationToken()
        with self._active_runs_lock:
            self._active_run_tokens.add(token)
        try:
            try:
                return await asyncio.to_thread(
                    self._run_blocking_entry,
                    task=task,
                    model_override=model_override,
                    conversation_id=conversation_id,
                    mode=mode,
                    user_id=user_id,
                    skip_memory=skip_memory,
                    allowed_servers=allowed_servers,
                    mode_framing_override=mode_framing_override,
                    preload_tools=preload_tools,
                    prefetch=prefetch,
                    debug_markdown_path=debug_markdown_path,
                    extra_tools=extra_tools,
                    observer=observer,
                    preserve_existing_usage=preserve_existing_usage,
                    event_type=event_type,
                    team_id=team_id,
                    capability_envelope=capability_envelope,
                    cancellation_token=token,
                    trigger_turn_id=trigger_turn_id,
                    heartbeat_tick_kind=heartbeat_tick_kind,
                    include_plans_index=include_plans_index,
                )
            except asyncio.CancelledError:
                token.cancel("async task cancelled")
                raise
        finally:
            with self._active_runs_lock:
                self._active_run_tokens.discard(token)

    def _run_blocking_entry(self, **kwargs) -> str:
        """Thread entry: bind RunContext, then run the blocking body.

        Modes overlap — there is no global run lock. Stdio MCP calls are
        serialized per server; workspace writes use ``memory_write_lock``.
        """
        token = kwargs.get("cancellation_token")
        if token is not None:
            token.raise_if_cancelled()
        return self._run_blocking(**kwargs)

    # Backward-compatible alias used by older tests.
    def _run_blocking_locked(self, **kwargs) -> str:
        kwargs.pop("preemptible", None)
        return self._run_blocking_entry(**kwargs)

    def _run_blocking(self, **kwargs) -> str:
        """Wrap the run body with durable run-log recording on every exit path.

        A fresh time-ordered ``run_id`` identifies this run in the run log.
        Run-scoped state (usage, ledger, cheap-workers flag) lives on a
        ``RunContext`` bound for this thread so overlapping modes stay isolated.
        """
        mode = kwargs.get("mode", RunMode.AUTONOMOUS)
        run_uid = uuid7_str()
        parent_ctx = get_run_context()
        parent_run_id = parent_ctx.run_id if parent_ctx else None
        preserve = bool(kwargs.get("preserve_existing_usage", False))

        if preserve and parent_ctx is not None and parent_ctx.usage_tracker is not None:
            usage_tracker = parent_ctx.usage_tracker
            subagent_ledger = parent_ctx.subagent_ledger
        elif preserve:
            # Heartbeat resets agent fallbacks then calls with preserve=True.
            usage_tracker = self._usage_tracker
            subagent_ledger = self._subagent_ledger
        else:
            usage_tracker = UsageTracker()
            subagent_ledger = []

        mode_value = getattr(mode, "value", str(mode))
        task_text = kwargs.get("task", "") or ""
        run_ctx = RunContext(
            run_id=run_uid,
            mode=mode_value,
            event_type=kwargs.get("event_type"),
            conversation_id=kwargs.get("conversation_id"),
            team_id=kwargs.get("team_id"),
            tick_id=self._current_tick_id,
            parent_run_id=parent_run_id,
            usage_tracker=usage_tracker,
            subagent_ledger=subagent_ledger,
            heartbeat_cheap_workers=(mode == RunMode.HEARTBEAT),
            cancellation_token=kwargs.get("cancellation_token"),
            task_preview=task_text[:200],
        )

        record = RunRecord(
            run_id=run_uid,
            agent_name=self.config.agent.name,
            mode=mode_value,
            parent_run_id=parent_run_id,
            tick_id=run_ctx.tick_id,
            event_type=kwargs.get("event_type"),
            conversation_id=kwargs.get("conversation_id"),
            team_id=kwargs.get("team_id"),
            user_id=kwargs.get("user_id"),
            trigger_turn_id=kwargs.get("trigger_turn_id"),
            task=task_text,
        )
        cap = kwargs.get("capability_envelope")
        if cap is not None:
            record.capability_role = getattr(cap.role, "value", None)
            record.capability_surface = getattr(cap.surface, "value", None)
        started = time.monotonic()
        token = kwargs.get("cancellation_token")
        with bind_run_context(run_ctx):
            self._active_runs.register(run_ctx, token or RunCancellationToken())
            try:
                result = self._run_blocking_inner(record, **kwargs)
                record.mark_success(result)
                return result
            except (RunCancelled, asyncio.CancelledError) as e:
                record.mark_cancelled(str(e) or "cancelled")
                raise
            except Exception as e:
                record.mark_error(e)
                raise
            finally:
                record.finalize_timing(time.monotonic() - started)
                self._finalize_run_record(record)
                self._active_runs.unregister(run_uid)

    def _run_blocking_inner(
        self,
        record: RunRecord,
        task: str,
        model_override=None,
        conversation_id: Optional[str] = None,
        mode: RunMode = RunMode.AUTONOMOUS,
        user_id: Optional[str] = None,
        skip_memory: bool = False,
        allowed_servers: Optional[list[str]] = None,
        mode_framing_override: str = "",
        preload_tools: Optional[list[str]] = None,
        prefetch: Optional[PrefetchSpec] = None,
        debug_markdown_path: Optional[Path] = None,
        extra_tools: Optional[list] = None,
        observer: Optional[AgentObserver] = None,
        preserve_existing_usage: bool = False,
        event_type: Optional[str] = None,
        team_id: Optional[str] = None,
        capability_envelope: Optional[CapabilityEnvelope] = None,
        cancellation_token: Optional[RunCancellationToken] = None,
        trigger_turn_id: Optional[str] = None,
        heartbeat_tick_kind: Optional[str] = None,
        include_plans_index: bool = True,
    ) -> str:
        token = cancellation_token or RunCancellationToken()
        token.raise_if_cancelled()
        run_started_at = time.monotonic()
        self.connect_mcp()
        run_profile = resolve_mode_profile(mode)
        if model_override is not None:
            model = model_override
        elif mode == RunMode.CHAT:
            chat_model_id = (
                self._model_id_for_role("chat") or self.config.agent.model
            )
            model = self._build_model(
                chat_model_id,
                role="chat",
                conversational=run_profile.conversational,
            )
        elif mode == RunMode.AUTONOMOUS:
            auto_model_id = (
                self._model_id_for_role("autonomous") or self.config.agent.model
            )
            model = self._build_model(
                auto_model_id,
                role="autonomous",
                conversational=run_profile.conversational,
            )
        else:
            model = self._build_model(
                self.config.agent.model,
                conversational=run_profile.conversational,
            )
        record.model = model.model_id if hasattr(model, "model_id") else str(model)
        record._model_obj = model
        active_doc_store = self._resolve_doc_store(team_id=team_id)

        _patched_reasoning_callbacks = False
        _original_reasoning_cb = None
        _original_reasoning_stream_cb = None
        if observer and hasattr(model, "_reasoning_callback"):
            _patched_reasoning_callbacks = True
            _original_reasoning_cb = model._reasoning_callback
            _original_reasoning_stream_cb = getattr(
                model, "_reasoning_stream_callback", None
            )

            def _composed_reasoning(text: str) -> None:
                if _original_reasoning_cb:
                    _original_reasoning_cb(text)
                try:
                    observer.on_reasoning_persist(text)
                except Exception:
                    logger.warning("Failed to persist reasoning message", exc_info=True)

            model._reasoning_callback = _composed_reasoning
            model._reasoning_stream_callback = observer.on_reasoning_stream

        _patched_retry_callback = False
        _original_retry_callback = None
        if observer and hasattr(model, "retry_callback"):
            _patched_retry_callback = True
            _original_retry_callback = getattr(model, "retry_callback", None)
            rate_limit_note_sent = False
            model_label = getattr(model, "model_id", "") or ""

            def _on_provider_retry(
                exc: BaseException, delay_s: float, attempt: int
            ) -> None:
                nonlocal rate_limit_note_sent
                activity = format_rate_limit_activity(model_label, delay_s)
                try:
                    observer.on_activity("thinking", activity, True)
                except Exception:
                    logger.warning(
                        "Failed to emit rate-limit activity update", exc_info=True
                    )
                if (
                    delay_s >= RATE_LIMIT_NOTE_MIN_DELAY_S
                    and not rate_limit_note_sent
                ):
                    rate_limit_note_sent = True
                    try:
                        observer.on_intermediate_end(uuid7_str(), RATE_LIMIT_NOTE)
                    except Exception:
                        logger.warning(
                            "Failed to persist rate-limit note", exc_info=True
                        )

            model.retry_callback = _on_provider_retry

        if not preserve_existing_usage:
            self.reset_usage_tracking()
        run_id = conversation_id or f"run_{uuid4().hex[:12]}"

        # Resolve mode profile and apply user config overrides
        profile = resolve_mode_profile(mode)
        override = self.config.modes.profiles.get(profile.name)
        if override:
            profile = apply_mode_override(profile, override)
        if capability_envelope is not None:
            logger.info(
                "Applying capability envelope: role=%s surface=%s capabilities=%s",
                capability_envelope.role.value,
                capability_envelope.surface.value,
                ",".join(
                    sorted(cap.value for cap in capability_envelope.allowed_capabilities)
                ),
            )
            profile = apply_capability_envelope(profile, capability_envelope)

        is_heartbeat = mode == RunMode.HEARTBEAT
        # heartbeat_cheap_workers is set on RunContext at bind time. Re-assert
        # here for nested preserve paths so delegated workers stay cheap.
        previous_cheap_workers = self._get_heartbeat_cheap_workers()
        self._set_heartbeat_cheap_workers(is_heartbeat)
        try:
            return self._continue_run_blocking_inner(
                record=record,
                task=task,
                model=model,
                profile=profile,
                conversation_id=conversation_id,
                mode=mode,
                user_id=user_id,
                skip_memory=skip_memory,
                allowed_servers=allowed_servers,
                mode_framing_override=mode_framing_override,
                preload_tools=preload_tools,
                prefetch=prefetch,
                debug_markdown_path=debug_markdown_path,
                extra_tools=extra_tools,
                observer=observer,
                preserve_existing_usage=preserve_existing_usage,
                event_type=event_type,
                team_id=team_id,
                capability_envelope=capability_envelope,
                token=token,
                trigger_turn_id=trigger_turn_id,
                run_started_at=run_started_at,
                active_doc_store=active_doc_store,
                is_heartbeat=is_heartbeat,
                run_id=run_id,
                include_plans_index=include_plans_index,
                heartbeat_tick_kind=heartbeat_tick_kind,
                _patched_reasoning_callbacks=_patched_reasoning_callbacks,
                _original_reasoning_cb=_original_reasoning_cb,
                _original_reasoning_stream_cb=_original_reasoning_stream_cb,
                _patched_retry_callback=_patched_retry_callback,
                _original_retry_callback=_original_retry_callback,
            )
        finally:
            self._set_heartbeat_cheap_workers(previous_cheap_workers)

    def _continue_run_blocking_inner(
        self,
        *,
        record: RunRecord,
        task: str,
        model,
        profile,
        conversation_id: Optional[str],
        mode: RunMode,
        user_id: Optional[str],
        skip_memory: bool,
        allowed_servers: Optional[list[str]],
        mode_framing_override: str,
        preload_tools: Optional[list[str]],
        prefetch: Optional[PrefetchSpec],
        debug_markdown_path: Optional[Path],
        extra_tools: Optional[list],
        observer: Optional[AgentObserver],
        preserve_existing_usage: bool,
        event_type: Optional[str],
        team_id: Optional[str],
        capability_envelope: Optional[CapabilityEnvelope],
        token: RunCancellationToken,
        trigger_turn_id: Optional[str],
        run_started_at: float,
        active_doc_store,
        is_heartbeat: bool,
        run_id: str,
        include_plans_index: bool = True,
        heartbeat_tick_kind: Optional[str] = None,
        _patched_reasoning_callbacks: bool,
        _original_reasoning_cb,
        _original_reasoning_stream_cb,
        _patched_retry_callback: bool,
        _original_retry_callback,
    ) -> str:
        # Merge profile preload tools with any explicit preload_tools.
        # Use dict.fromkeys for stable, first-seen dedup so order is
        # deterministic across runs (explicit preloads take precedence).
        mode_preloads = list(profile.preload_tools)
        if mode_preloads:
            preload_tools = list(dict.fromkeys((preload_tools or []) + mode_preloads))

        # --- Trivial message fast path (regex only, no LLM) ---
        is_trivial = is_trivial_message(task)

        display = get_display()

        def _status_cb(status: str, message: Optional[str], active: bool):
            if observer:
                observer.on_activity(status, message, active)

        def _progress_cb(event: ProgressEvent) -> None:
            if observer:
                observer.on_progress(event)

        # --- Build tools ---

        emit_progress(observer, "building_tools", "loading available tools")
        (
            all_tools,
            deferred_tool_directory,
            agent_ref,
            preloaded_names,
            tool_closeables,
        ) = self._build_agent_tools(
            profile,
            user_id=user_id,
            allowed_servers=allowed_servers,
            preload_tools=preload_tools,
            conversation_id=conversation_id,
            run_id=run_id,
            team_id=team_id,
            doc_store=active_doc_store,
            run_mode=mode.value,
            event_type=event_type or "",
            cancellation_token=token,
            observer=observer,
        )
        if extra_tools:
            all_tools.extend(extra_tools)
        emit_progress(
            observer,
            "building_tools",
            f"{len(all_tools)} tools ready",
            state="complete",
        )

        # Conversational runs inject verbatim history as structured memory
        # steps. Append-only until a watermark compaction folds older turns
        # into an internal continuity summary (see chat_compaction).
        history_turns: list[dict] = []
        all_turns: list[dict] = []
        history_summary = ""
        history_compacted = False
        history_compaction_reason: Optional[str] = None
        if profile.conversational and conversation_id:
            all_turns = self._resolve_conversation_turns(
                conversation_id,
                limit=HISTORY_FETCH_LIMIT,
                trigger_turn_id=trigger_turn_id,
            )
            compaction_cfg = self.config.chat_compaction
            prior_compaction = (
                load_compaction(self._workspace, conversation_id)
                if compaction_cfg.enabled
                else None
            )
            built = build_injectable_history(all_turns, compaction=prior_compaction)
            history_turns = built.injected_turns
            history_summary = built.summary
            history_compacted = built.compacted
            history_compaction_reason = built.compaction_reason

        # Build haystack for entity-file matching: recent history + current task.
        entity_haystack_parts = [
            str(t.get("content", "")) for t in history_turns[-12:]
        ]
        entity_haystack_parts.append(task)
        entity_haystack = "\n".join(p for p in entity_haystack_parts if p)

        # Build system prompt (static, cacheable) + dynamic context (per-turn).
        emit_progress(observer, "building_prompt", "assembling context")
        # Quest-work heartbeats omit the plans index (inbox is the plan surface).
        effective_include_plans = (
            include_plans_index if is_heartbeat else True
        )
        system_prompt, dynamic_context = self._build_system_prompt(
            task=task,
            profile=profile,
            conversation_id=conversation_id,
            deferred_tool_directory=deferred_tool_directory,
            user_id=user_id,
            mode_framing_override=mode_framing_override,
            preloaded_tool_names=preloaded_names,
            team_id=team_id,
            doc_store=active_doc_store,
            trigger_turn_id=trigger_turn_id,
            include_plans_index=effective_include_plans,
            entity_haystack=entity_haystack,
        )
        emit_progress(observer, "building_prompt", "prompt ready", state="complete")

        # Hard compaction backstop (chat only): if the assembled prompt would
        # exceed the hard fraction of context, fold older turns now so the
        # reply can fit. Soft compaction runs after the reply instead.
        if (
            profile.conversational
            and conversation_id
            and self.config.chat_compaction.enabled
            and all_turns
        ):
            compaction_cfg = self.config.chat_compaction
            est_prompt = estimate_chat_prompt_tokens(
                system_prompt=system_prompt,
                dynamic_context=dynamic_context,
                task=task,
                injected_turns=history_turns,
                summary=history_summary,
            )
            level = should_compact(
                est_prompt,
                context_tokens=compaction_cfg.context_tokens,
                soft_fraction=compaction_cfg.soft_fraction,
                hard_fraction=compaction_cfg.hard_fraction,
            )
            if level == "hard":
                emit_progress(observer, "compacting_history", "compacting chat history")
                utility = self._build_model(
                    self._utility_model_id(),
                    role="utility",
                )
                # Do not name this `record` — that shadows the RunRecord param
                # and blows up later when compaction returns None.
                compaction = run_compaction_locked(
                    self._workspace,
                    conversation_id,
                    all_turns,
                    utility,
                    reason="hard",
                    keep_recent=compaction_cfg.keep_recent_turns,
                    model_id=self._utility_model_id(),
                )
                if compaction is not None:
                    built = build_injectable_history(all_turns, compaction=compaction)
                    history_turns = built.injected_turns
                    history_summary = built.summary
                    history_compacted = True
                    history_compaction_reason = "hard"
                    # Rebuild dynamic context is unnecessary — summary lives in
                    # history steps, not the dynamic prompt. Re-estimate only.
                emit_progress(
                    observer,
                    "compacting_history",
                    "history compacted" if compaction else "compaction skipped",
                    state="complete",
                )

        # Assemble the effective task: dynamic context + prefetched data + request
        context_parts: list[str] = []
        if dynamic_context:
            context_parts.append(dynamic_context)
        if profile.allowed_capabilities is not None:
            from .security.policy import describe_capabilities

            capability_note = describe_capabilities(profile.allowed_capabilities)
            if capability_note:
                context_parts.append(capability_note)
        if prefetch:
            emit_progress(observer, "prefetching_context", "loading referenced context")
            prefetch_context = resolve_prefetch(self._deferred_tools, prefetch)
            if prefetch_context:
                context_parts.append(prefetch_context)
            emit_progress(
                observer,
                "prefetching_context",
                "context loaded" if prefetch_context else "no context loaded",
                state="complete",
            )

        if context_parts:
            effective_task = (
                "\n\n---\n\n".join(context_parts)
                + f"\n\n---\n\n## Current request\n{task}"
            )
        else:
            effective_task = task

        step_callback = self._build_step_callback(
            _status_cb,
            display,
            progress_callback=_progress_cb,
        )
        main_max_steps = profile.max_steps
        # NOTE: tool persistence/emission (observer.on_step_persist) is
        # deliberately NOT registered as a smolagents step_callback. smolagents
        # fires step_callbacks inside _finalize_step *before* yielding the
        # ActionStep. The step's streamed commentary is only flushed (persisted +
        # end-of-stream) when we receive that yielded ActionStep in the run loop
        # below. Persisting tools in the callback would therefore order tool
        # events ahead of a commentary message that chronologically came first.
        # Instead we call observer.on_step_persist from the ActionStep branch of
        # the streaming loop, after _flush_intermediate, to keep the order correct.
        step_callbacks = [step_callback]

        from .tools.observation_policy import to_observation_policy

        obs_policy = to_observation_policy(self.config.observations)
        agent = _SanitizedToolCallingAgent(
            tools=all_tools,
            model=model,
            max_steps=main_max_steps,
            stream_outputs=bool(observer),
            step_callbacks=step_callbacks,
            logger=create_logger(display=display),
            observation_policy=obs_policy,
            workspace=self._workspace,
            run_id=run_id,
            cancellation_token=token,
            plain_task_messages=profile.conversational,
            action_gate_mode=self.config.ask_controller.gate_mode,
            action_gate_observer=lambda tool_name, category: (
                self._run_log.record_action_gate_observation(
                    run_id=run_id,
                    tool_name=tool_name,
                    category=category,
                )
            ),
        )
        agent_ref["agent"] = agent
        record._agent = agent

        agent.prompt_templates["system_prompt"] = build_tool_calling_system_prompt(
            system_prompt,
            conversational=profile.conversational,
            observation_policy=obs_policy,
        )

        if debug_markdown_path:
            try:
                write_run_debug_markdown_preamble(
                    Path(debug_markdown_path),
                    task=task,
                    effective_task=effective_task,
                    full_system_prompt=agent.prompt_templates["system_prompt"],
                    run_id=run_id,
                    mode=mode,
                    heartbeat_tick_kind=heartbeat_tick_kind,
                )
            except OSError as e:
                logger.warning("Failed to write debug markdown preamble: %s", e)

        # In chat mode, inject recent turns as structured steps so the model
        # sees user/assistant pairs verbatim (plus an optional continuity
        # summary when a watermark compaction is active).
        has_history = False
        if history_turns or history_summary:
            history_steps = build_history_steps(
                history_turns, summary=history_summary
            )
            agent.memory.steps.extend(history_steps)
            has_history = True
            logger.info(
                "Injected %d history steps from %d turns"
                "%s for conversation %s",
                len(history_steps),
                len(history_turns),
                " (+compaction summary)" if history_summary else "",
                conversation_id,
            )

        use_reset = not has_history

        # Measure what actually went into this turn's prompt so history policy
        # and compaction thresholds can be tuned against real cache and token
        # behavior. The row is inserted before the run and updated with usage
        # afterwards, so cancelled and errored turns are still measured.
        chat_turn: Optional[ChatTurnRecord] = None
        if mode == RunMode.CHAT and conversation_id:
            try:
                chat_turn = build_chat_turn_record(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    agent_name=self.config.agent.name,
                    trigger_turn_id=trigger_turn_id,
                    model=record.model,
                    all_turns=all_turns,
                    injected_turns=history_turns,
                    history_steps=len(agent.memory.steps),
                    system_prompt=agent.prompt_templates["system_prompt"],
                    dynamic_context=dynamic_context,
                    task=task,
                    compacted=history_compacted,
                    compaction_reason=history_compaction_reason,
                )
                self._run_log.record_chat_turn(chat_turn)
            except Exception:
                logger.debug("Failed to measure chat turn", exc_info=True)

        # Overlap title generation with the agent run so the first reply is
        # not blocked on naming. Title uses the user message only; we await
        # the future just before persisting so email subjects see the name.
        naming_future = None
        if mode == RunMode.CHAT and conversation_id:
            ouro_client = self._get_ouro_client()
            if ouro_client is not None:
                naming_future = start_name_conversation_if_needed(
                    ouro_client,
                    conversation_id,
                    task,
                    lambda: self._build_model(
                        self._utility_model_id(),
                        role="utility",
                        max_completion_tokens=64,
                    ),
                )

        token.raise_if_cancelled()
        emit_progress(
            observer,
            "running_agent",
            f"running {mode.value} agent (max_steps={main_max_steps})",
        )
        if observer:
            try:
                final_result = None
                streamer = FinalAnswerStreamer()
                intermediate_streamer = IntermediateContentStreamer()
                current_intermediate_message_id: str | None = None
                # Guards against double tool persistence: smolagents re-yields the
                # last ActionStep when max_steps is reached (see _run_stream), and
                # we must not emit/persist that step's tools twice.
                last_persisted_step: object | None = None

                def _flush_intermediate(*, drop: bool = False) -> None:
                    """Close out the current step's commentary stream, if any.

                    Called on every step boundary. When ``drop`` is True the
                    buffered text is discarded instead of persisted (used when a
                    step ends with no real content — typically the final-answer
                    step whose only "content" is the answer itself, streamed
                    separately via on_stream_chunk).
                    """
                    nonlocal current_intermediate_message_id
                    if current_intermediate_message_id is None:
                        intermediate_streamer.reset()
                        return
                    full_text = intermediate_streamer.flush()
                    msg_id = current_intermediate_message_id
                    current_intermediate_message_id = None
                    if drop:
                        try:
                            observer.on_intermediate_drop(msg_id)
                        except Exception:
                            logger.warning(
                                "observer.on_intermediate_drop failed",
                                exc_info=True,
                            )
                        return
                    if not full_text.strip():
                        return
                    try:
                        observer.on_intermediate_end(msg_id, full_text)
                    except Exception:
                        logger.warning(
                            "observer.on_intermediate_end failed",
                            exc_info=True,
                        )

                for event in agent.run(effective_task, stream=True, reset=use_reset):
                    if isinstance(event, ChatMessageStreamDelta):
                        chunk = streamer.consume(event)
                        if chunk:
                            observer.on_stream_chunk(chunk)
                        inter_chunk = intermediate_streamer.consume(event)
                        if inter_chunk:
                            if current_intermediate_message_id is None:
                                current_intermediate_message_id = uuid7_str()
                            try:
                                observer.on_intermediate_chunk(
                                    current_intermediate_message_id, inter_chunk
                                )
                            except Exception:
                                logger.warning(
                                    "observer.on_intermediate_chunk failed",
                                    exc_info=True,
                                )
                    elif isinstance(event, ActionStep):
                        # Step boundary. First flush this step's streamed
                        # commentary (persist it + signal end-of-stream) unless
                        # the step ended on a final_answer (in which case the
                        # final-answer stream is already covering the user-
                        # facing reply via on_stream_chunk + on_result_ready).
                        # Flushing before tool persistence below guarantees the
                        # commentary message is ordered ahead of this step's
                        # tools, matching the chronological order in which the
                        # model produced them.
                        _flush_intermediate(
                            drop=bool(getattr(event, "is_final_answer", False))
                        )
                        # Now emit/persist this step's tool calls. Done here
                        # (rather than as a smolagents step_callback) so tools
                        # are always ordered after the commentary that preceded
                        # them. ``is`` guard dedupes the max-steps re-yield.
                        if event is not last_persisted_step:
                            last_persisted_step = event
                            try:
                                observer.on_step_persist(event)
                            except Exception:
                                logger.warning(
                                    "observer.on_step_persist failed",
                                    exc_info=True,
                                )
                    elif isinstance(event, FinalAnswerStep):
                        final_result = event.output

                # Safety net: if the run terminated without a trailing ActionStep
                # (e.g. AgentMaxStepsError path), make sure we don't leak buffered
                # commentary that was streamed but never flushed for persistence.
                _flush_intermediate()
                result = final_result if final_result is not None else ""
            finally:
                for closeable in tool_closeables:
                    try:
                        closeable.close()
                    except Exception:
                        logger.warning("Failed to close tool session", exc_info=True)
        else:
            try:
                result = agent.run(effective_task, reset=use_reset)
            finally:
                for closeable in tool_closeables:
                    try:
                        closeable.close()
                    except Exception:
                        logger.warning("Failed to close tool session", exc_info=True)
        token.raise_if_cancelled()
        emit_progress(
            observer, "running_agent", "agent loop complete", state="complete"
        )

        if debug_markdown_path:
            try:
                append_run_debug_markdown_trace(
                    Path(debug_markdown_path), agent, str(result)
                )
            except OSError as e:
                logger.warning("Failed to append debug markdown trace: %s", e)

        tool_summary = extract_tool_summary(agent, for_persistence=True)

        # Finish overlapping naming before persist so email can use the title.
        await_conversation_naming(naming_future, conversation_id=conversation_id)

        if observer:
            try:
                emit_progress(observer, "persisting_response", "saving response")
                observer.on_result_ready(str(result))
                emit_progress(
                    observer,
                    "persisting_response",
                    "response saved",
                    state="complete",
                )
            except Exception as e:
                emit_progress(
                    observer,
                    "persisting_response",
                    str(e),
                    state="failed",
                )
                logger.warning("observer.on_result_ready failed: %s", e)

        if conversation_id and profile.append_conversation_turns:
            try:
                append_conversation_turn(self._workspace, conversation_id, "user", task)
                append_conversation_turn(
                    self._workspace,
                    conversation_id,
                    "assistant",
                    str(result),
                    tool_summary=tool_summary or None,
                )
            except Exception as e:
                logger.warning("Failed to append conversation turn: %s", e)

        # Heartbeat tick-summary drives run-log columns + memory gating.
        tick_summary = None
        if is_heartbeat:
            from .modes.heartbeat import parse_heartbeat_tick_summary

            tick_summary = parse_heartbeat_tick_summary(str(result))
            if tick_summary["is_pass"]:
                record.preflight_intent = "pass"
                record.preflight_complexity = "pass"
            else:
                record.preflight_intent = "act"
                record.preflight_complexity = (
                    f"priority:{tick_summary['selected_priority']}"
                    if tick_summary["selected_priority"] is not None
                    else "moderate"
                )
            record.worth_remembering = tick_summary["worth_remembering"]

        def _do_post_run():
            if tick_summary is not None:
                store_semantic = bool(tick_summary["worth_remembering"])
                want_episode = not tick_summary["is_pass"]
                memory_notes = (
                    tick_summary["memory_notes"] if store_semantic else None
                )
            else:
                store_semantic = not is_trivial
                want_episode = False
                memory_notes = None
            if (
                not profile.skip_post_reflection
                and not skip_memory
                and (store_semantic or want_episode)
                and (
                    capability_envelope is None
                    or capability_envelope.allows(Capability.MEMORY_WRITE)
                )
            ):
                self._post_run_reflect(
                    task,
                    str(result),
                    tool_summary,
                    mode=mode,
                    user_id=user_id,
                    run_id=run_id,
                    event_type=event_type,
                    team_id=team_id,
                    doc_store=active_doc_store,
                    conversation_id=conversation_id,
                    memory_notes=memory_notes,
                    store_semantic=store_semantic,
                )

            # Soft chat compaction: after a successful reply, if the prompt was
            # near the soft threshold, fold older turns into an internal
            # continuity summary so the next turns stay under budget without
            # sliding-window cache busts.
            if (
                mode == RunMode.CHAT
                and conversation_id
                and self.config.chat_compaction.enabled
                and all_turns
                and not token.cancelled
            ):
                try:
                    self._maybe_soft_compact_chat(
                        conversation_id=conversation_id,
                        all_turns=all_turns,
                        history_turns=history_turns,
                        history_summary=history_summary,
                        system_prompt=system_prompt,
                        dynamic_context=dynamic_context,
                        task=task,
                    )
                except Exception:
                    logger.warning(
                        "Soft chat compaction failed for %s",
                        conversation_id,
                        exc_info=True,
                    )

        def _run_post_run_background() -> None:
            try:
                if token.cancelled:
                    return
                _do_post_run()
            except Exception:
                logger.warning("Post-run background task failed", exc_info=True)

        threading.Thread(
            target=_run_post_run_background,
            name=f"ouro-post-run-{run_id}",
            daemon=True,
        ).start()

        usage = collect_run_usage(agent, model, self._active_usage_tracker())
        memory_ledger = self.memory.usage_ledger() or None
        if memory_ledger:
            usage.num_embedding_calls = sum(
                int(getattr(u, "num_embedding_calls", 0) or getattr(u, "num_api_calls", 0) or 0)
                for name, u in memory_ledger
                if name == "embeddings"
            )
            # Generation totals should exclude embedding calls that shared the
            # tracker via the memory wrap.
            generation_rows = [
                g
                for g in (usage.generations or [])
                if g.get("call_kind", "generation") != "embedding"
            ]
            usage.num_generation_calls = len(generation_rows) or max(
                0, usage.num_api_calls - usage.num_embedding_calls
            )
        logger.info(
            "Run usage:\n%s",
            format_usage_breakdown(usage, self._active_subagent_ledger(), memory_ledger),
        )
        _display = display or get_display()
        ledger = self._active_subagent_ledger() or None
        _display.queue_run_summary(
            usage=usage,
            duration_s=max(0.0, time.monotonic() - run_started_at),
            subagent_ledger=ledger,
            memory_ledger=memory_ledger,
        )

        if chat_turn is not None:
            chat_turn.duration_s = max(0.0, time.monotonic() - run_started_at)
            apply_chat_turn_usage(chat_turn, usage)
            self._run_log.update_chat_turn_usage(chat_turn)
            logger.info(format_chat_turn(chat_turn))

        # Populate the run record's usage now that it's computed for display;
        # the wrapper's finally block snapshots steps and writes the record.
        record.set_usage(usage)
        record.set_subagent_ledger(self._active_subagent_ledger() or None)
        record.set_memory_ledger(memory_ledger)

        if _patched_reasoning_callbacks:
            model._reasoning_callback = _original_reasoning_cb
            model._reasoning_stream_callback = _original_reasoning_stream_cb
        if _patched_retry_callback and hasattr(model, "retry_callback"):
            model.retry_callback = _original_retry_callback

        return str(result)

    async def heartbeat(self) -> Optional[str]:
        from .modes.heartbeat import run_heartbeat

        self.reset_usage_tracking()
        return await run_heartbeat(self)

    async def force_planning_heartbeat(
        self, goal: str = "", team_id: str | None = None
    ) -> Optional[str]:
        from .modes.heartbeat import force_planning_heartbeat

        return await force_planning_heartbeat(self, goal=goal, team_id=team_id)

    def _run_dream_scope(
        self,
        *,
        team_id: str | None = None,
        dry_run: bool = False,
        mode: str = "manual",
        tick_id: str | None = None,
        doc_store=None,
    ) -> dict:
        """Run dream for one memory scope and ledger it like other modes.

        Writes a ``runs.db`` row with ``mode="dream"``, isolated LLM usage, and
        optional memory ledger — matching chat/autonomous/heartbeat cost
        accounting. Existing ``protected/data/dream_runs/*.json`` audits are unchanged.
        """
        scope = team_id or "shared"
        if doc_store is None:
            doc_store = (
                self.doc_store_for(team_id) if team_id else self.doc_store
            )

        run_uid = uuid7_str()
        parent_ctx = get_run_context()
        parent_run_id = parent_ctx.run_id if parent_ctx else None
        dream_tracker = UsageTracker()
        run_ctx = RunContext(
            run_id=run_uid,
            mode="dream",
            team_id=team_id,
            tick_id=tick_id or getattr(self, "_current_tick_id", None),
            parent_run_id=parent_run_id,
            usage_tracker=dream_tracker,
            task_preview=f"dream [{mode}] scope={scope}",
        )
        record = RunRecord(
            run_id=run_uid,
            agent_name=self.config.agent.name,
            mode="dream",
            parent_run_id=parent_run_id,
            tick_id=run_ctx.tick_id,
            team_id=team_id,
            task=(
                f"dream [{mode}] scope={scope}"
                + (" (dry-run)" if dry_run else "")
            ),
        )

        # Dedicated tracker so dream LLM cost never pollutes / is lost in the
        # shared run tracker (dream is not a smolagents loop).
        active_runs = getattr(self, "_active_runs", None)
        with bind_run_context(run_ctx):
            if active_runs is not None:
                active_runs.register(run_ctx, RunCancellationToken())
            try:
                return OuroAgent._run_dream_scope_inner(
                    self,
                    record=record,
                    dream_tracker=dream_tracker,
                    team_id=team_id,
                    dry_run=dry_run,
                    mode=mode,
                    doc_store=doc_store,
                    scope=scope,
                    run_uid=run_uid,
                )
            finally:
                if active_runs is not None:
                    active_runs.unregister(run_uid)

    def _run_dream_scope_inner(
        self,
        *,
        record: RunRecord,
        dream_tracker: UsageTracker,
        team_id: str | None,
        dry_run: bool,
        mode: str,
        doc_store,
        scope: str,
        run_uid: str,
    ) -> dict:
        from .memory.dream import run_dream

        model = self._build_model(
            self._utility_model_id(),
            role="utility",
            usage_tracker=dream_tracker,
        )
        record.model = getattr(model, "model_id", "") or ""
        record._model_obj = model

        started = time.monotonic()
        try:
            summary = run_dream(
                workspace=self.config.agent.workspace,
                backend=self.memory,
                agent_id=self.config.agent.name,
                config=self.config.memory,
                model=model,
                doc_store=doc_store,
                team_id=team_id,
                dry_run=dry_run,
                mode=mode,
                agent=self,
                run_id=run_uid,
            )
            record.mark_success(json.dumps(summary, default=str))
            return summary
        except Exception as e:
            record.mark_error(e)
            raise
        finally:
            record.finalize_timing(time.monotonic() - started)
            try:
                usage = RunUsage.from_tracker(
                    dream_tracker,
                    model_id=getattr(model, "model_id", "") or "",
                )
                memory_ledger = None
                try:
                    memory_ledger = self.memory.usage_ledger() or None
                except Exception:
                    pass
                if memory_ledger:
                    usage.num_embedding_calls = sum(
                        int(
                            getattr(u, "num_embedding_calls", 0)
                            or getattr(u, "num_api_calls", 0)
                            or 0
                        )
                        for name, u in memory_ledger
                        if name == "embeddings"
                    )
                record.set_usage(usage)
                record.set_memory_ledger(memory_ledger)
                logger.info(
                    "Dream usage (%s):\n%s",
                    scope,
                    format_usage_breakdown(usage, None, memory_ledger),
                )
            except Exception:
                logger.debug("Failed to collect dream usage", exc_info=True)
            self._finalize_run_record(record)

    def dream(
        self,
        team_id: str | None = None,
        *,
        dry_run: bool = False,
        mode: str = "manual",
    ) -> dict[str, dict]:
        """Run the dream cycle (memory maintenance) immediately.

        If *team_id* is provided, only that team is processed. Otherwise runs
        across shared scope and all configured teams. Each scope writes a
        ``mode=dream`` run-log row; a shared ``tick_id`` groups the cycle.
        """
        tick_id = uuid7_str()
        results: dict[str, dict] = {}

        if team_id:
            results[team_id] = self._run_dream_scope(
                team_id=team_id,
                dry_run=dry_run,
                mode=mode,
                tick_id=tick_id,
            )
        else:
            results["shared"] = self._run_dream_scope(
                dry_run=dry_run,
                mode=mode,
                tick_id=tick_id,
                doc_store=self.doc_store,
            )
            for tid, store in sorted(self._team_doc_stores.items()):
                results[tid] = self._run_dream_scope(
                    team_id=tid,
                    dry_run=dry_run,
                    mode=mode,
                    tick_id=tick_id,
                    doc_store=store,
                )
        return results

    def _finalize_run_record(self, record: RunRecord) -> None:
        """Snapshot steps + usage onto the record and persist it.

        Runs in the wrapper's ``finally`` so it fires on success, error, and
        cancellation. Best-effort: never raises. On the success path usage and
        ledgers are already populated; on error/cancel we fill in whatever the
        run reached before failing.
        """
        try:
            cfg = self.config.run_log
            agent = record._agent

            if cfg.capture_steps and not record.steps and agent is not None:
                try:
                    cap = cfg.max_observation_chars if cfg.capture_observations else 0
                    steps = extract_run_steps(agent, max_observation_chars=cap)
                    if not cfg.capture_observations:
                        for step in steps:
                            step.observations = None
                    if not cfg.capture_reasoning:
                        for step in steps:
                            step.reasoning = None
                    record.set_steps(steps)
                except Exception:
                    logger.debug("Failed to extract run steps", exc_info=True)

            # Fill usage/ledgers if the run errored before they were collected.
            if record.usage_json is None and agent is not None and record._model_obj:
                try:
                    record.set_usage(
                        collect_run_usage(agent, record._model_obj, self._active_usage_tracker())
                    )
                except Exception:
                    logger.debug("Failed to collect usage for run record", exc_info=True)
                if record.subagent_ledger_json is None:
                    record.set_subagent_ledger(self._active_subagent_ledger() or None)
                if record.memory_ledger_json is None:
                    try:
                        record.set_memory_ledger(self.memory.usage_ledger() or None)
                    except Exception:
                        pass

            self._run_log.write(record)
        except Exception:
            logger.warning("Failed to finalize run record", exc_info=True)
