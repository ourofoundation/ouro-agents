import json
import logging
import os
import time
from datetime import date, datetime, timezone
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
from .config import (
    MCPServerConfig,
    OuroAgentsConfig,
    ReasoningConfig,
    RunMode,
    merge_reasoning,
)
from .display import OuroDisplay, create_logger, get_display
from .memory import create_memory_backend
from .memory.conversation_state import (
    ConversationState,
    load_state,
    save_state,
    update_state,
)
from .memory.ouro_docs import DocStore, LocalDocStore, OuroDocStore
from .memory.reflection import (
    apply_reflection,
    should_reflect_for_conversation,
    write_daily_log,
)
from .memory.tools import make_memory_tools

from .modes import ModeProfile, apply_mode_override, resolve_mode_profile
from .observer import AgentObserver
from .skills import get_skill_directory, load_startup_skills
from .soul import build_prompt
from .subagents.context import SubAgentUsage
from .subagents.delegate_utils import (
    delegate_success_payload,
    normalize_return_mode,
    resolve_auto_return_mode,
    summarize_delegate_text,
    validate_delegate_result,
)
from .subagents.preflight import PreflightResult, parse_preflight_result
from .subagents.reflector import (
    ReflectionResult,
    build_run_reflection_task,
    normalize_daily_log_entry,
    parse_reflection_result,
)
from .tool_prompt import build_tool_calling_system_prompt
from .tools.agent_base import SanitizedToolCallingAgent as _SanitizedToolCallingAgent
from .tools.python_tool import make_python_tool
from .tools.scheduler_tools import make_scheduler_tools
from .tools.skills_tools import make_load_skill_tool
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
    append_conversation_turn,
    build_history_steps,
    conversation_file,
    extract_tool_summary,
    format_conversation_turns,
    load_conversation_turns,
)
from .utils.debug import (
    append_run_debug_markdown_trace,
    write_run_debug_markdown_preamble,
)
from .utils.streaming import FinalAnswerStreamer

if TYPE_CHECKING:
    from .modes.planning import PlanCycle
    from .subagents.context import SubAgentContext

logger = logging.getLogger(__name__)

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
        self._usage_tracker = UsageTracker()
        self.memory = create_memory_backend(
            config.memory,
            usage_tracker=self._usage_tracker,
        )
        self._workspace = config.agent.workspace
        self._subagent_ledger: list[tuple[str, SubAgentUsage]] = []
        self.model = self._build_model(config.agent.model)

        self._mcp_contexts: list = []
        self._deferred_tools: dict = {}
        self._deferred_tools_by_raw_name: dict = {}
        self._deferred_index: list[dict] = []
        self._mcp_connected = False

        self.doc_store: DocStore = LocalDocStore(
            workspace=config.agent.workspace,
            agent_name=config.agent.name,
        )

        from .scheduler import AgentScheduler

        self.scheduler = AgentScheduler(
            config.agent.workspace / "data" / "scheduled_tasks.json"
        )

        self._load_custom_profiles()

    def _load_custom_profiles(self) -> None:
        """Load custom subagent profiles from workspace or config path."""
        from .subagents.profiles import DELEGATABLE_PROFILES, build_profile_registry

        custom_dir = None
        if self.config.subagents.custom_profiles_dir:
            custom_dir = Path(self.config.subagents.custom_profiles_dir)
            if not custom_dir.is_absolute():
                custom_dir = self.config.agent.workspace / custom_dir
        else:
            default_dir = self.config.agent.workspace / "subagents"
            if default_dir.exists():
                custom_dir = default_dir

        if custom_dir:
            merged = build_profile_registry(custom_dir)
            DELEGATABLE_PROFILES.update(merged)
            logger.info(
                "Profile registry: %d delegatable profiles (%s)",
                len(DELEGATABLE_PROFILES),
                ", ".join(DELEGATABLE_PROFILES.keys()),
            )

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """Remove YAML frontmatter (---...---) from markdown text."""
        if not text.startswith("---"):
            return text
        end = text.find("---", 3)
        if end == -1:
            return text
        return text[end + 3 :].lstrip("\n")

    def _refresh_platform_context(self) -> None:
        """Fetch profile, org, and team info from the Ouro MCP server and cache it.

        Called at startup and on heartbeat. Other runs read from cache.
        """
        context: dict = {
            "profile": None,
            "organizations": [],
            "teams": [],
            "base_url": os.getenv("OURO_FRONTEND_URL") or os.getenv("OURO_BASE_URL") or "https://ouro.foundation",
        }

        me_tool = self._deferred_tools.get("ouro:get_me")
        if me_tool:
            try:
                raw = me_tool()
                context["profile"] = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as e:
                logger.warning("Platform context: failed to fetch profile: %s", e)

        org_tool = self._deferred_tools.get("ouro:get_organizations")
        if org_tool:
            try:
                raw = org_tool()
                data = json.loads(raw) if isinstance(raw, str) else raw
                context["organizations"] = (
                    data.get("results", data) if isinstance(data, dict) else data
                )
            except Exception as e:
                logger.warning("Platform context: failed to fetch orgs: %s", e)

        teams_tool = self._deferred_tools.get("ouro:get_teams")
        if teams_tool:
            try:
                raw = teams_tool()
                data = json.loads(raw) if isinstance(raw, str) else raw
                context["teams"] = (
                    data.get("results", data) if isinstance(data, dict) else data
                )
            except Exception as e:
                logger.warning("Platform context: failed to fetch teams: %s", e)

        cache_path = self._workspace / "data" / "platform_context.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(context, indent=2))
        logger.info(
            "Refreshed platform context: %d orgs, %d teams",
            len(context["organizations"]),
            len(context["teams"]),
        )

    def _load_platform_context(self) -> str:
        """Load cached platform context for inclusion in the system prompt."""
        from .platform_context_prompt import format_platform_context_for_prompt

        return format_platform_context_for_prompt(self._workspace)

    def _load_working_memory(self) -> str:
        """Load working memory and today's daily log for the system prompt."""
        parts: list[str] = []
        name = self.config.agent.name
        today = date.today().isoformat()

        content = self.doc_store.read(f"MEMORY:{name}")
        if content:
            parts.append(content)
        daily_content = self.doc_store.read(f"DAILY:{name}:{today}")
        if daily_content:
            parts.append(f"## Today's Log ({today})\n{daily_content}")

        return "\n\n".join(parts)

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
                lines.append(
                    f"- {task.name} [{task.schedule} {task.timezone}] status={status} runs={task.run_count}"
                )
            remaining = len(enabled_tasks) - min(len(enabled_tasks), 8)
            if remaining > 0:
                lines.append(f"- ... and {remaining} more enabled scheduled task(s)")
        else:
            lines.append("- No enabled scheduled tasks.")

        if disabled_count:
            lines.append(f"- Disabled scheduled tasks: {disabled_count}")

        return "\n".join(lines)

    def _is_anthropic_model(self, model_id: str) -> bool:
        return model_id.startswith("anthropic/")

    def _resolve_reasoning(
        self,
        *,
        subagent_profile: Optional[str] = None,
        heartbeat: bool = False,
    ) -> Optional[ReasoningConfig]:
        """Merge global + optional heartbeat overlay + optional subagent override."""
        layers: list[Optional[ReasoningConfig]] = [self.config.reasoning]
        if heartbeat:
            layers.append(self.config.heartbeat.reasoning)
        if subagent_profile:
            override = self.config.subagents.overrides.get(subagent_profile)
            if override and override.reasoning is not None:
                layers.append(override.reasoning)
        return merge_reasoning(*layers)

    def _build_openrouter_extra_body(
        self,
        model_id: str,
        reasoning: Optional[ReasoningConfig],
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
            if r:
                body["reasoning"] = r

        return body if body else None

    def _default_tool_choice(self, model_id: str) -> Optional[str]:
        # OpenRouter routes for MiniMax reject smolagents' default
        # `tool_choice="required"`; fall back to `auto` for compatibility.
        if model_id.startswith("minimax/"):
            return "auto"
        return None

    def _build_model(
        self,
        model_id: str,
        *,
        reasoning: Optional[ReasoningConfig] = None,
        subagent_profile: Optional[str] = None,
        heartbeat: bool = False,
        usage_tracker: Optional[UsageTracker] = None,
    ) -> TrackedOpenAIModel:
        model_kwargs = {}
        resolved = (
            reasoning
            if reasoning is not None
            else self._resolve_reasoning(
                subagent_profile=subagent_profile,
                heartbeat=heartbeat,
            )
        )
        extra_body = self._build_openrouter_extra_body(model_id, resolved)
        if extra_body:
            model_kwargs["extra_body"] = extra_body
        tool_choice = self._default_tool_choice(model_id)
        if tool_choice is not None:
            model_kwargs["tool_choice"] = tool_choice

        return TrackedOpenAIModel(
            model_id=model_id,
            api_base="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            tracker=usage_tracker or self._usage_tracker,
            reasoning_callback=get_display().reasoning,
            **model_kwargs,
        )

    def _get_ouro_client(self):
        """Build or return a cached Ouro SDK client for the Python sandbox.

        Uses the same env vars as the MCP server (OURO_API_KEY, OURO_BASE_URL).
        Returns None if credentials are unavailable so the sandbox degrades
        gracefully.
        """
        if hasattr(self, "_ouro_client"):
            return self._ouro_client

        api_key = os.getenv("OURO_API_KEY")
        if not api_key:
            logger.warning(
                "OURO_API_KEY not set — Ouro SDK unavailable in Python sandbox"
            )
            self._ouro_client = None
            return None

        try:
            from ouro import Ouro

            self._ouro_client = Ouro(
                api_key=api_key,
                base_url=os.getenv("OURO_BASE_URL"),
            )
            logger.info("Ouro SDK client created for Python sandbox")
        except Exception as e:
            logger.warning("Failed to create Ouro SDK client: %s", e)
            self._ouro_client = None

        return self._ouro_client

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
                self.config.heartbeat.model or self.config.agent.model,
                heartbeat=True,
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

    def _init_doc_store(self) -> None:
        """Upgrade to OuroDocStore if agent.org_id and agent.team_id are configured."""
        agent_cfg = self.config.agent
        if not agent_cfg.org_id or not agent_cfg.team_id:
            logger.info(
                "OuroDocStore: org_id/team_id not configured, using LocalDocStore"
            )
            return
        self.doc_store = OuroDocStore(
            agent_name=agent_cfg.name,
            org_id=agent_cfg.org_id,
            team_id=agent_cfg.team_id,
            client=self._get_ouro_client(),
            registry_path=self._workspace / "data" / "doc_registry.json",
        )
        self._sync_workspace_docs()
        self._load_identity_from_ouro()

    def _sync_workspace_docs(self) -> None:
        """Bidirectional sync between local workspace files and Ouro posts."""
        if not isinstance(self.doc_store, OuroDocStore):
            return
        from .memory.workspace_sync import sync_workspace

        result = sync_workspace(
            workspace=self._workspace,
            doc_store=self.doc_store,
            agent_name=self.config.agent.name,
        )
        if result.pushed:
            logger.info("Workspace sync pushed: %s", ", ".join(result.pushed))
        if result.pulled:
            logger.info("Workspace sync pulled: %s", ", ".join(result.pulled))
        if result.errors:
            for err in result.errors:
                logger.warning("Workspace sync error: %s", err)

    def _load_identity_from_ouro(self) -> None:
        """Load soul, notes, and heartbeat from Ouro posts (falls back to local files)."""
        if not isinstance(self.doc_store, OuroDocStore):
            return
        name = self.config.agent.name
        soul = self.doc_store.read(f"SOUL:{name}")
        if soul:
            self.soul = soul
        notes = self.doc_store.read(f"NOTES:{name}")
        if notes:
            self.notes = notes

    def connect_mcp(self) -> None:
        """Connect to all configured MCP servers once. Safe to call multiple times."""
        if self._mcp_connected:
            return

        for server in self.config.mcp_servers:
            self._connect_one_server(server)
        self._mcp_connected = True

        try:
            self._init_doc_store()
        except Exception as e:
            logger.warning("Failed to initialize OuroDocStore: %s", e)

        try:
            self._refresh_platform_context()
        except Exception as e:
            logger.warning("Failed to refresh platform context at startup: %s", e)

    @staticmethod
    def _patch_tool_inputs(mcp_tool) -> None:
        """Fix mcpadapt's schema conversion for nullable/optional MCP params.

        mcpadapt doesn't translate anyOf: [{type: X}, {type: null}] into
        smolagents' nullable flag, causing validation errors when the LLM
        sends null or omits optional parameters.  We also remove anyOf after
        extracting type info so smolagents' get_tool_json_schema doesn't
        crash on entries missing a "type" key (e.g. from Any).
        """
        for schema in getattr(mcp_tool, "inputs", {}).values():
            any_of = schema.get("anyOf", [])
            has_null = any(item.get("type") == "null" for item in any_of)
            has_default = "default" in schema

            if has_null:
                non_null = [item for item in any_of if item.get("type") != "null"]
                if non_null:
                    schema["type"] = non_null[0].get("type", "string")
                else:
                    schema.setdefault("type", "string")
                schema["nullable"] = True
                del schema["anyOf"]
            elif has_default:
                schema["nullable"] = True

    def _connect_one_server(self, server: MCPServerConfig) -> None:
        if server.transport == "stdio":
            if not server.command:
                return
            try:
                from mcp import StdioServerParameters

                env = dict(server.env or {})
                env.setdefault("WORKSPACE_ROOT", str(self._workspace.resolve()))
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
                for mcp_tool in collection.tools:
                    self._patch_tool_inputs(mcp_tool)
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
                    self._deferred_tools_by_raw_name.setdefault(
                        mcp_tool.name, []
                    ).append(qualified_name)
                logger.info("Connected to MCP server: %s", server.name)
            except Exception as e:
                logger.error("Failed to connect to MCP server %s: %s", server.name, e)
        elif server.transport == "streamable-http":
            raise NotImplementedError(
                f"MCP transport 'streamable-http' is not yet implemented "
                f"(server: {server.name}). Use 'stdio' transport instead."
            )

    def close(self) -> None:
        """Shut down all MCP server connections."""
        for ctx in self._mcp_contexts:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        self._mcp_contexts.clear()
        self._deferred_tools.clear()
        self._deferred_tools_by_raw_name.clear()
        self._deferred_index.clear()
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

    def _build_agent_tools(
        self,
        profile: ModeProfile,
        user_id: Optional[str] = None,
        allowed_servers: Optional[list[str]] = None,
        preload_tools: Optional[list[str]] = None,
        conversation_state: Optional[ConversationState] = None,
        conversation_id: Optional[str] = None,
        run_id: str = "",
    ):
        """Build the tool list and directory string for a single run.

        Returns (all_tools, deferred_tool_directory, agent_ref, preloaded_names).
        ``preloaded_names`` lists the raw call names of tools that were eagerly
        resolved and added to ``all_tools`` so the agent can use them without
        calling ``load_tool`` first.
        """
        deferred_tools = self._deferred_tools
        deferred_index = self._deferred_index

        if profile.restricted_servers:
            servers = (
                set(allowed_servers)
                if allowed_servers
                else set(profile.default_servers)
            )
            deferred_index = [
                item for item in self._deferred_index if item["server"] in servers
            ]
            filtered_names = {item["tool"] for item in deferred_index}
            deferred_tools = {
                k: v for k, v in self._deferred_tools.items() if k in filtered_names
            }

        agent_self = self
        agent_ref: dict = {}

        from .tools.mcp_tools import make_load_tool

        load_tool = make_load_tool(
            deferred_tools,
            deferred_index,
            agent_ref,
            resolve_fn=agent_self._resolve_tool_name,
        )

        memory_tools = make_memory_tools(
            self.memory,
            self.config.agent.name,
            user_id=user_id,
            workspace=self.config.agent.workspace,
            doc_store=self.doc_store,
        )
        if profile.memory_tool_filter is not None:
            allowed = set(profile.memory_tool_filter)
            memory_tools = [t for t in memory_tools if t.name in allowed]

        ouro_client = self._get_ouro_client()
        python_tool, _executor = make_python_tool(
            workspace=self.config.agent.workspace,
            ouro_client=ouro_client,
        )
        load_skill = make_load_skill_tool(self.config.agent.workspace)
        scheduler_tools = (
            make_scheduler_tools(self.scheduler)
            if not profile.restricted_servers
            else []
        )

        # Build the delegate tool for subagent dispatch
        from .subagents.profiles import DELEGATABLE_PROFILES

        subagent_names = list(DELEGATABLE_PROFILES.keys())

        _subagent_names_str = ", ".join(subagent_names)

        def _do_delegate(
            subagent: str,
            task: str,
            asset_refs: Optional[list[str]] = None,
        ) -> tuple:
            """Shared delegation logic. Returns (result, profile) or (None, None)."""
            profile = DELEGATABLE_PROFILES.get(subagent)
            if not profile:
                return None, None

            result = agent_self._run_subagent(
                profile,
                task,
                conversation_state=conversation_state,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
                asset_refs=asset_refs or [],
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
        def delegate(tasks: list) -> str:
            """Delegate one or more tasks to specialized subagents. Multiple tasks run in parallel automatically.

            Use this to offload heavy or focused work (web research, sub-task
            execution, planning) so your main context stays clean. Each subagent
            runs independently and returns a structured JSON handoff. By default
            the parent sees a short summary plus asset metadata (name, description,
            asset_id). The subagent saves its full output as an Ouro post (or other
            asset type) automatically.

            Args:
                tasks: List of task specs. Each is a dict with keys:
                    - subagent (str, required): Name of the subagent (see subagent directory in system prompt).
                    - task (str, required): A clear, self-contained description of what the subagent should do.
                    - asset_refs (list[str], optional): Ouro asset UUIDs to pass as input context.
                    - return_mode (str, optional): summary_only, full_text, or auto. Defaults to the subagent profile setting.

            Example single:  [{"subagent": "research", "task": "Find recent papers on X"}]
            Example multi:   [{"subagent": "research", "task": "Find papers on X"}, {"subagent": "writer", "task": "Draft intro section"}]
            """
            from concurrent.futures import ThreadPoolExecutor, as_completed

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

            if len(tasks) == 1:
                return json.dumps(_run_one(tasks[0]))

            outputs = [None] * len(tasks)
            with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
                future_to_idx = {
                    pool.submit(_run_one, spec): i for i, spec in enumerate(tasks)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        outputs[idx] = future.result()
                    except Exception as e:
                        outputs[idx] = {
                            "status": "error",
                            "subagent": tasks[idx].get("subagent", "?"),
                            "return_mode": normalize_return_mode(
                                tasks[idx].get("return_mode", "")
                            ),
                            "error": str(e),
                        }

            return json.dumps(outputs)

        delegate.description += f"\n\nAvailable subagents: {_subagent_names_str}"

        delegate_tools = [] if profile.restricted_servers else [delegate]

        _has_memory_filter = profile.memory_tool_filter is not None
        if _has_memory_filter:
            all_tools = list(memory_tools)
        else:
            all_tools = (
                list(memory_tools)
                + scheduler_tools
                + delegate_tools
                + [load_tool, load_skill, python_tool]
            )

        preloaded_names: list[str] = []
        for qualified_name in preload_tools or []:
            resolved, err = self._resolve_tool_name(qualified_name)
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

        if _has_memory_filter:
            deferred_tool_directory = ""
        else:
            deferred_tool_directory = "\n".join(
                f"- {item['tool']}: {item['description'][:80]}"
                for item in deferred_index
            )

        return all_tools, deferred_tool_directory, agent_ref, preloaded_names

    def _build_system_prompt(
        self,
        task: str,
        profile: ModeProfile,
        conversation_id: Optional[str],
        deferred_tool_directory: str,
        user_id: Optional[str] = None,
        conversation_state: Optional[ConversationState] = None,
        mode_framing_override: str = "",
        preloaded_tool_names: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        """Build the system prompt and dynamic context.

        Returns (system_prompt, dynamic_context) where dynamic_context should
        be prepended to the task message for prompt-cache-friendly layout.
        """
        conversation_context = ""
        conversation_state_text = ""
        if profile.load_conversation_state and conversation_state:
            conversation_state_text = conversation_state.format_for_prompt()
        elif conversation_id and not profile.lightweight:
            turns = load_conversation_turns(self._workspace, conversation_id, limit=24)
            conversation_context = format_conversation_turns(
                turns, summarize_fn=self._summarize_turns
            )

        skills_text = "" if profile.lightweight else self.skills
        skill_directory = "" if profile.lightweight else self.skill_directory

        working_memory_parts = [self._load_working_memory()]
        if profile.load_scheduled_tasks:
            working_memory_parts.append(self._load_scheduled_task_awareness())
        working_memory = "\n\n".join(part for part in working_memory_parts if part)

        user_model_text = ""
        if user_id:
            user_model_text = self.doc_store.read(f"USER:{user_id}")

        from .memory.context_loader import load_entity_context
        from .modes.planning import PlanStore, format_plans_index_for_prompt

        entity_context_text = load_entity_context(
            self.config.agent.workspace,
            conversation_state=conversation_state,
            task=task,
            doc_store=self.doc_store,
            agent_name=self.config.agent.name,
        )

        plan_store = PlanStore(self.config.agent.workspace / "plans")
        plans_index_text = format_plans_index_for_prompt(plan_store.load_all_active())

        from .subagents.profiles import DELEGATABLE_PROFILES

        if not profile.lightweight and DELEGATABLE_PROFILES:
            subagent_directory = "\n".join(
                f"- **{p.name}**: {p.description}"
                for p in DELEGATABLE_PROFILES.values()
            )
        else:
            subagent_directory = ""

        return build_prompt(
            soul=self.soul,
            notes=self.notes,
            skills=skills_text,
            profile=profile,
            skill_directory=skill_directory,
            working_memory=working_memory,
            conversation_context=conversation_context,
            conversation_state=conversation_state_text,
            user_model=user_model_text,
            entity_context=entity_context_text,
            deferred_tool_directory=deferred_tool_directory,
            subagent_directory=subagent_directory,
            mode_framing_override=mode_framing_override,
            platform_context=self._load_platform_context(),
            chat_conversation_id=(
                conversation_id if profile.include_chat_conversation_id else None
            ),
            preloaded_tool_names=preloaded_tool_names,
            plans_index=plans_index_text,
        )

    def _resolve_subagent_model(
        self,
        profile,
        *,
        usage_tracker: Optional[UsageTracker] = None,
    ) -> "TrackedOpenAIModel":
        """Resolve the model for a subagent profile using the override cascade."""
        override = self.config.subagents.overrides.get(profile.name)
        model_id = (
            profile.model_override
            or (override.model if override else None)
            or self.config.subagents.default_model
            or self.config.agent.model
        )
        return self._build_model(
            model_id,
            subagent_profile=profile.name,
            usage_tracker=usage_tracker,
        )

    def _apply_profile_overrides(self, profile):
        """Apply config overrides (max_steps, etc.) to a profile."""
        override = self.config.subagents.overrides.get(profile.name)
        if override and override.max_steps is not None:
            return profile.model_copy(update={"max_steps": override.max_steps})
        return profile

    def _build_subagent_context(
        self,
        profile,
        model,
        task: str = "",
        conversation_state: Optional[ConversationState] = None,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        asset_refs: Optional[list[str]] = None,
        usage_tracker: Optional[UsageTracker] = None,
    ) -> "SubAgentContext":
        from .subagents.context import SubAgentContext

        compactor_model = self._build_model(
            self.config.heartbeat.model or self.config.agent.model,
            heartbeat=True,
            usage_tracker=usage_tracker,
        )

        ouro_client = (
            self._get_ouro_client()
            if getattr(profile, "needs_python_tool", False)
            else None
        )

        return SubAgentContext(
            workspace=self._workspace,
            backend=self.memory,
            agent_id=self.config.agent.name,
            memory_config=self.config.memory,
            model=model,
            compactor_model=compactor_model,
            user_id=user_id,
            conversation_state=conversation_state,
            conversation_id=conversation_id,
            deferred_tools=self._deferred_tools,
            deferred_index=self._deferred_index,
            run_id=run_id,
            asset_refs=list(asset_refs or []),
            memory_scopes=getattr(profile, "memory_scopes", []) or [],
            ouro_client=ouro_client,
            record_subagent_usage=self._record_subagent_usage,
        )

    def _record_subagent_usage(self, name: str, usage: SubAgentUsage) -> None:
        self._subagent_ledger.append((name, usage))

    def _run_subagent(
        self,
        profile,
        task: str,
        conversation_state: Optional[ConversationState] = None,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        asset_refs: Optional[list[str]] = None,
    ):
        """Build context and dispatch a subagent through the unified runner.

        Returns a SubAgentResult with .text, .success, .error, and .usage fields.
        """
        from .subagents.runner import run_subagent

        effective_profile = self._apply_profile_overrides(profile)
        subagent_usage_tracker = MirroredUsageTracker(
            UsageTracker(),
            mirrors=[self._usage_tracker],
        )
        model = self._resolve_subagent_model(
            profile,
            usage_tracker=subagent_usage_tracker,
        )

        ctx = self._build_subagent_context(
            effective_profile,
            model,
            task=task,
            conversation_state=conversation_state,
            conversation_id=conversation_id,
            user_id=user_id,
            run_id=run_id,
            asset_refs=asset_refs,
            usage_tracker=subagent_usage_tracker,
        )

        return run_subagent(effective_profile, task, ctx)

    def _run_subagents_parallel(
        self,
        tasks: list[tuple],
        conversation_state: Optional[ConversationState] = None,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
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
                mirrors=[self._usage_tracker],
            )
            model = self._resolve_subagent_model(
                profile,
                usage_tracker=subagent_usage_tracker,
            )

            ctx = self._build_subagent_context(
                effective_profile,
                model,
                task=task_str,
                conversation_state=conversation_state,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
                asset_refs=extra.get("asset_refs"),
                usage_tracker=subagent_usage_tracker,
            )
            dispatch_list.append((effective_profile, task_str, ctx))

        return run_subagents_parallel(dispatch_list)

    def _build_step_callback(
        self,
        status_callback: Optional[RunStatusCallback],
        display: Optional[OuroDisplay] = None,
    ) -> Callable[[ActionStep], None]:
        return build_step_callback(
            self._usage_tracker,
            status_callback=status_callback,
            display=display,
        )

    def _run_preflight(
        self,
        task: str,
        conv_state: Optional[ConversationState] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        asset_refs: Optional[list[str]] = None,
        display: Optional[OuroDisplay] = None,
        status_callback: Optional[RunStatusCallback] = None,
    ) -> PreflightResult:
        """Run the preflight subagent as a visible step 0.

        Consolidates classification, memory retrieval, and planning into
        a single subagent call that returns structured JSON.
        """
        from .subagents.profiles import PREFLIGHT

        _display = display or get_display()
        _display.step("Step 0: preflight")
        if status_callback:
            try:
                status_callback("thinking", "is analyzing the task...", True)
            except Exception:
                logger.exception("Failed to emit preflight status")

        t0 = time.monotonic()
        result = self._run_subagent(
            PREFLIGHT,
            task,
            conversation_state=conv_state,
            user_id=user_id,
            run_id=run_id,
            asset_refs=asset_refs,
        )
        duration_s = time.monotonic() - t0

        if result.usage and result.usage.total_tokens:
            _display.token_summary(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                step_number=0,
                duration_s=duration_s,
                cost_usd=result.usage.cost_usd,
            )

        if not result.success:
            logger.warning("Preflight subagent failed: %s", result.error)
            return PreflightResult()

        return parse_preflight_result(result.text)

    def _run_reflection(
        self,
        task: str,
        conversation_state: Optional[ConversationState] = None,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: str = "",
        display: Optional[OuroDisplay] = None,
        status_callback: Optional[RunStatusCallback] = None,
    ) -> Optional[ReflectionResult]:
        """Run the reflector subagent as a visible step.

        Mirrors ``_run_preflight``: shows a display step, tracks usage via the
        subagent ledger, and returns a parsed ``ReflectionResult`` (or None on
        failure).
        """
        from .subagents.profiles import REFLECTOR

        _display = display or get_display()
        _display.step("reflecting...")
        if status_callback:
            try:
                status_callback("thinking", "is reflecting...", True)
            except Exception:
                logger.exception("Failed to emit reflection status")

        t0 = time.monotonic()
        result = self._run_subagent(
            REFLECTOR,
            task,
            conversation_state=conversation_state,
            conversation_id=conversation_id,
            user_id=user_id,
            run_id=run_id,
        )
        duration_s = time.monotonic() - t0

        if result.usage and result.usage.total_tokens:
            _display.token_summary(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                duration_s=duration_s,
                cost_usd=result.usage.cost_usd,
            )

        if not result.success:
            logger.warning("Reflector subagent failed: %s", result.error)
            return None

        return parse_reflection_result(result.text)

    def _maybe_reflect_post_turn(
        self,
        conv_state: Optional[ConversationState],
        conversation_id: Optional[str],
        user_id: Optional[str] = None,
        run_id: str = "",
    ) -> None:
        """Run mid-session reflection after the turn if enough turns have passed."""
        if not conversation_id:
            return
        conversations_dir = self.config.agent.workspace / "conversations"
        if not should_reflect_for_conversation(
            conversations_dir,
            conversation_id,
            conv_state,
            self.config.memory.mid_session_reflection_interval,
        ):
            return

        try:
            reflection_result = self._run_reflection(
                "Reflect on the recent conversation turns and extract what is worth remembering.",
                conversation_state=conv_state,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
            )
            if not reflection_result:
                return
            apply_reflection(
                reflection_result,
                self.memory,
                agent_id=self.config.agent.name,
                user_id=user_id,
                conversation_id=conversation_id,
                workspace=self.config.agent.workspace,
                conversations_dir=conversations_dir,
                conversation_state=conv_state,
                doc_store=self.doc_store,
            )
            logger.info(
                "Post-turn reflection for %s (turn %d): %d facts, %d prefs",
                conversation_id,
                conv_state.turn_count if conv_state else 0,
                len(reflection_result.facts_to_store),
                len(reflection_result.user_preferences),
            )
        except Exception as e:
            logger.warning("Post-turn reflection failed for %s: %s", conversation_id, e)

    def _post_run_reflect(
        self,
        task: str,
        result: str,
        tool_summary: list[dict],
        mode: RunMode = RunMode.AUTONOMOUS,
        user_id: Optional[str] = None,
        run_id: str = "",
    ) -> None:
        """Run reflection after an autonomous/event run via the reflector subagent.

        Extracts curated facts (with categories, importance, asset refs) and
        writes a daily log entry. Runs as a proper subagent so usage is tracked
        and the step is visible in the display.
        """
        reflection_task = build_run_reflection_task(
            task=task,
            result=str(result),
            tool_summary=tool_summary,
            run_mode=mode.value,
        )

        try:
            reflection = self._run_reflection(
                reflection_task,
                user_id=user_id,
                run_id=run_id,
            )
            if not reflection:
                return

            for fact in reflection.facts_to_store:
                text = fact.get("text", "")
                if not text:
                    continue
                metadata = {
                    "category": fact.get("category", "fact"),
                    "importance": fact.get("importance", 0.5),
                    "source": f"run-reflection:{run_id}",
                }
                asset_refs = fact.get("asset_refs", [])
                if asset_refs:
                    metadata["asset_refs"] = ",".join(asset_refs)
                try:
                    self.memory.add(
                        text,
                        agent_id=self.config.agent.name,
                        user_id=user_id,
                        run_id=run_id,
                        metadata=metadata,
                    )
                except Exception as e:
                    logger.warning("Failed to store run-reflection fact: %s", e)

            if reflection.daily_log_entry:
                write_daily_log(
                    self.config.agent.workspace,
                    normalize_daily_log_entry(reflection.daily_log_entry, mode.value),
                    doc_store=self.doc_store,
                    agent_name=self.config.agent.name,
                )

            logger.info(
                "Post-run reflection: %d facts, daily=%s",
                len(reflection.facts_to_store),
                bool(reflection.daily_log_entry),
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
    ) -> str:
        run_started_at = time.monotonic()
        self.connect_mcp()
        model = model_override or self.model

        _original_reasoning_cb = None
        if observer and hasattr(model, "_reasoning_callback"):
            _original_reasoning_cb = model._reasoning_callback

            def _composed_reasoning(text: str) -> None:
                if _original_reasoning_cb:
                    _original_reasoning_cb(text)
                try:
                    observer.on_reasoning_persist(text)
                except Exception:
                    logger.warning("Failed to persist reasoning message", exc_info=True)

            model._reasoning_callback = _composed_reasoning

        if not preserve_existing_usage:
            self._usage_tracker.reset()
            self.memory.reset_usage()
            self._subagent_ledger.clear()
        run_id = conversation_id or f"run_{uuid4().hex[:12]}"

        # Resolve mode profile and apply user config overrides
        profile = resolve_mode_profile(mode)
        override = self.config.modes.overrides.get(profile.name)
        if override:
            profile = apply_mode_override(profile, override)

        # Merge profile preload tools with any explicit preload_tools
        mode_preloads = list(profile.preload_tools)
        if mode_preloads:
            preload_tools = list(set((preload_tools or []) + mode_preloads))

        # --- Conversation state ---
        conv_state: Optional[ConversationState] = None
        if profile.load_conversation_state and conversation_id:
            conversations_dir = self.config.agent.workspace / "conversations"
            conv_state = load_state(conversations_dir, conversation_id)

        # --- Trivial message fast path (regex only, no LLM) ---
        is_trivial = is_trivial_message(task)

        # --- Build tools ---
        all_tools, deferred_tool_directory, agent_ref, preloaded_names = (
            self._build_agent_tools(
                profile,
                user_id=user_id,
                allowed_servers=allowed_servers,
                preload_tools=preload_tools,
                conversation_state=conv_state,
                conversation_id=conversation_id,
                run_id=run_id,
            )
        )
        if extra_tools:
            all_tools.extend(extra_tools)

        # Build system prompt (static, cacheable) + dynamic context (per-turn).
        system_prompt, dynamic_context = self._build_system_prompt(
            task=task,
            profile=profile,
            conversation_id=conversation_id,
            deferred_tool_directory=deferred_tool_directory,
            user_id=user_id,
            conversation_state=conv_state,
            mode_framing_override=mode_framing_override,
            preloaded_tool_names=preloaded_names,
        )

        # --- Step 0: Preflight subagent (visible) ---
        display = get_display()
        preflight: Optional[PreflightResult] = None

        def _status_cb(status: str, message: Optional[str], active: bool):
            if observer:
                observer.on_activity(status, message, active)

        if not is_trivial and not skip_memory and not profile.skip_preflight:
            preflight = self._run_preflight(
                task,
                conv_state=conv_state,
                user_id=user_id,
                run_id=run_id,
                asset_refs=prefetch.asset_ids if prefetch else None,
                display=display,
                status_callback=_status_cb,
            )
            logger.info(
                "Preflight: intent=%s complexity=%s worth_remembering=%s briefing=%d plan=%d",
                preflight.intent,
                preflight.complexity,
                preflight.worth_remembering,
                len(preflight.briefing),
                len(preflight.plan),
            )

        # Assemble the effective task: dynamic context + prefetched data + preflight + request
        context_parts: list[str] = []
        if dynamic_context:
            context_parts.append(dynamic_context)
        if prefetch:
            prefetch_context = resolve_prefetch(self._deferred_tools, prefetch)
            if prefetch_context:
                context_parts.append(prefetch_context)
        if preflight and preflight.briefing:
            context_parts.append(f"## Context Briefing\n{preflight.briefing}")
        if preflight and preflight.plan:
            context_parts.append(f"## Execution Plan\n{preflight.plan}")

        if context_parts:
            effective_task = (
                "\n\n---\n\n".join(context_parts)
                + f"\n\n---\n\n## Current request\n{task}"
            )
        else:
            effective_task = task

        step_callback = self._build_step_callback(_status_cb, display)
        main_max_steps = profile.max_steps
        compactor_model = self._build_model(
            self.config.heartbeat.model or self.config.agent.model,
            heartbeat=True,
        )
        step_callbacks = [step_callback]
        if observer:
            step_callbacks.append(observer.on_step_persist)

        agent = _SanitizedToolCallingAgent(
            tools=all_tools,
            model=model,
            max_steps=main_max_steps,
            stream_outputs=bool(observer),
            step_callbacks=step_callbacks,
            logger=create_logger(display=display),
            compactor_model=compactor_model,
            is_chat_mode=(mode in (RunMode.CHAT, RunMode.CHAT_REPLY)),
        )
        agent_ref["agent"] = agent

        agent.prompt_templates["system_prompt"] = build_tool_calling_system_prompt(
            system_prompt
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
                    preflight=preflight,
                )
            except OSError as e:
                logger.warning("Failed to write debug markdown preamble: %s", e)

        # In chat mode, inject recent turns as structured steps so the model
        # sees user/assistant pairs verbatim.
        has_history = False
        if profile.load_conversation_state and conversation_id:
            turns = load_conversation_turns(self._workspace, conversation_id, limit=8)
            if turns:
                history_steps = build_history_steps(turns)
                agent.memory.steps.extend(history_steps)
                has_history = True
                logger.info(
                    "Injected %d history steps from %d recent turns for conversation %s",
                    len(history_steps),
                    len(turns),
                    conversation_id,
                )

        use_reset = not has_history

        if observer:
            final_result = None
            streamer = FinalAnswerStreamer()
            for event in agent.run(effective_task, stream=True, reset=use_reset):
                if isinstance(event, ChatMessageStreamDelta):
                    chunk = streamer.consume(event)
                    if chunk:
                        observer.on_stream_chunk(chunk)
                elif isinstance(event, FinalAnswerStep):
                    final_result = event.output
            result = final_result if final_result is not None else ""
        else:
            result = agent.run(effective_task, reset=use_reset)

        if debug_markdown_path:
            try:
                append_run_debug_markdown_trace(
                    Path(debug_markdown_path), agent, str(result)
                )
            except OSError as e:
                logger.warning("Failed to append debug markdown trace: %s", e)

        tool_summary = extract_tool_summary(agent, for_persistence=True)

        if observer:
            try:
                observer.on_result_ready(str(result))
            except Exception as e:
                logger.warning("observer.on_result_ready failed: %s", e)

        def _do_post_run():
            worth_remembering = preflight.worth_remembering if preflight else not is_trivial
            if not profile.skip_post_reflection and not skip_memory and worth_remembering:
                self._post_run_reflect(
                    task,
                    str(result),
                    tool_summary,
                    mode=mode,
                    user_id=user_id,
                    run_id=run_id,
                )
            if conversation_id and profile.append_conversation_turns:
                append_conversation_turn(self._workspace, conversation_id, "user", task)
                append_conversation_turn(
                    self._workspace,
                    conversation_id,
                    "assistant",
                    str(result),
                    tool_summary=tool_summary or None,
                )

            if profile.update_conversation_state and conversation_id:
                try:
                    state_model = self._build_model(
                        self.config.heartbeat.model or self.config.agent.model,
                        heartbeat=True,
                    )
                    new_conv_state = update_state(conv_state, task, str(result), state_model)
                    conversations_dir = self.config.agent.workspace / "conversations"
                    save_state(conversations_dir, conversation_id, new_conv_state)
                    logger.info(
                        "Updated conversation state for %s: topic=%s, turn=%d",
                        conversation_id,
                        new_conv_state.current_topic,
                        new_conv_state.turn_count,
                    )
                except Exception as e:
                    logger.warning("Failed to update conversation state: %s", e)
                    new_conv_state = conv_state

                # Mid-session reflection (post-turn): curate memories after responding
                self._maybe_reflect_post_turn(
                    conv_state=new_conv_state,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    run_id=run_id,
                )

        # Run reflection and state updates in a background thread
        import asyncio
        asyncio.create_task(asyncio.to_thread(_do_post_run))

        usage = collect_run_usage(agent, model, self._usage_tracker)
        memory_ledger = self.memory.usage_ledger() or None
        logger.info(
            "Run usage:\n%s",
            format_usage_breakdown(usage, self._subagent_ledger, memory_ledger),
        )
        _display = display or get_display()
        ledger = self._subagent_ledger or None
        _display.queue_run_summary(
            usage=usage,
            duration_s=max(0.0, time.monotonic() - run_started_at),
            subagent_ledger=ledger,
            memory_ledger=memory_ledger,
        )

        self._log_run(
            task,
            result,
            model.model_id if hasattr(model, "model_id") else str(model),
            mode,
            usage=usage,
        )

        if _original_reasoning_cb is not None:
            model._reasoning_callback = _original_reasoning_cb

        return str(result)

    async def heartbeat(self) -> Optional[str]:
        from .modes.heartbeat import run_heartbeat

        return await run_heartbeat(self)

    async def force_planning_heartbeat(self, goal: str = "") -> Optional[str]:
        from .modes.heartbeat import force_planning_heartbeat

        return await force_planning_heartbeat(self, goal=goal)

    async def force_review_heartbeat(self, plan_id: str | None = None) -> Optional[str]:
        from .modes.heartbeat import force_review_heartbeat

        return await force_review_heartbeat(self, plan_id=plan_id)

    async def handle_plan_feedback(self, event_run) -> Optional[str]:
        """Handle feedback on a plan post from an incoming event."""
        from .modes.planning import PlanStore, run_review_heartbeat

        prov = event_run.provenance
        if not prov or not prov.plan_cycle:
            return None

        plan_store = PlanStore(self.config.agent.workspace / "plans")
        matched = plan_store.load_by_post_id(prov.plan_cycle.post_id)

        if matched and matched.status in ("pending_review", "active"):
            hb_model_id = self.config.heartbeat.model or self.config.agent.model
            hb_model = self._build_model(hb_model_id, heartbeat=True)
            proactive_cfg = self.config.heartbeat.proactive
            servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

            reviewed = await run_review_heartbeat(
                self,
                hb_model,
                plan_store,
                matched,
                servers,
                inline_feedback=event_run.feedback_text or event_run.task,
                reply_parent_id=event_run.reply_parent_id,
                thread_parent_id=event_run.thread_parent_id,
                prefetch=event_run.prefetch if not event_run.prefetch.empty else None,
            )
            if reviewed:
                logger.info("Plan updated via event feedback (cycle %s)", reviewed.id)
            return reviewed.plan_text if reviewed else None

        return await self.run(
            task=event_run.task,
            mode=event_run.mode,
            user_id=event_run.user_id,
            preload_tools=(
                list(event_run.preload_tools) if event_run.preload_tools else None
            ),
        )

    def _log_run(
        self,
        task: str,
        result: str,
        model_name: str,
        mode: RunMode,
        usage: Optional[RunUsage] = None,
    ):
        """Append a line to the run log (JSONL)."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": mode.value,
            "task_summary": task[:200] + ("..." if len(task) > 200 else ""),
            "model": model_name,
            "result_summary": str(result)[:200]
            + ("..." if len(str(result)) > 200 else ""),
        }
        if usage:
            entry["usage"] = usage.dict()
        log_path = self.config.agent.workspace / "runs.jsonl"

        # Ensure workspace exists
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
