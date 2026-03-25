import json
import logging
import os
import re
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
from smolagents.memory import TaskStep
from smolagents.monitoring import Timing

from .classify import PreflightResult, is_trivial_message, parse_preflight_result
from .config import MCPServerConfig, OuroAgentsConfig, RunMode
from .artifacts import fetch_asset_content
from .memory import create_memory_backend
from .memory.conversation_state import (
    ConversationState,
    load_state,
    save_state,
    update_state,
)
from .memory.reflection import (
    apply_reflection,
    should_reflect_for_conversation,
    write_daily_log,
)
from .memory.tools import make_memory_tools
from .memory.user_model import load_user_model
from .notes import load_notes
from .skills import load_all_skills
from .soul import build_prompt, load_soul
from .subagents.context import SubAgentUsage
from .subagents.delegate_utils import (
    delegate_success_payload,
    normalize_return_mode,
    resolve_auto_return_mode,
    summarize_delegate_text,
    validate_delegate_result,
)
from .tools.python_tool import make_python_tool
from .tools.scheduler_tools import make_scheduler_tools
from .usage import (
    RunUsage,
    TrackedOpenAIModel,
    UsageTracker,
    collect_run_usage,
    format_usage_breakdown,
)

if TYPE_CHECKING:
    from .subagents.context import SubAgentContext

logger = logging.getLogger(__name__)

RunStatusCallback = Callable[[str, Optional[str], bool], None]
RunResponseCallback = Callable[[str], None]


def _extract_streamed_answer_text(arguments_blob: str) -> Optional[str]:
    try:
        parsed = json.loads(arguments_blob)
        if isinstance(parsed, dict) and "answer" in parsed:
            answer = parsed["answer"]
            if isinstance(answer, str):
                return answer
            return json.dumps(answer)
    except Exception:
        pass

    match = re.search(r'"answer"\s*:\s*', arguments_blob)
    if not match:
        return None

    idx = match.end()
    if idx >= len(arguments_blob):
        return ""

    if arguments_blob[idx] != '"':
        return arguments_blob[idx:].strip()

    idx += 1
    chars: list[str] = []
    escape = False

    while idx < len(arguments_blob):
        ch = arguments_blob[idx]
        idx += 1

        if escape:
            if ch == "n":
                chars.append("\n")
            elif ch == "r":
                chars.append("\r")
            elif ch == "t":
                chars.append("\t")
            elif ch == "b":
                chars.append("\b")
            elif ch == "f":
                chars.append("\f")
            elif ch == "u" and idx + 4 <= len(arguments_blob):
                hex_value = arguments_blob[idx : idx + 4]
                if len(hex_value) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", hex_value):
                    chars.append(chr(int(hex_value, 16)))
                    idx += 4
                else:
                    break
            else:
                chars.append(ch)
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            return "".join(chars)
        chars.append(ch)

    return "".join(chars)


class _FinalAnswerStreamer:
    def __init__(self):
        self._tool_names: dict[int, str] = {}
        self._arguments_by_index: dict[int, str] = {}
        self._streamed_text = ""

    def consume(self, delta: ChatMessageStreamDelta) -> Optional[str]:
        tool_calls = delta.tool_calls or []
        emitted: list[str] = []

        for tool_call in tool_calls:
            index = tool_call.index or 0
            function = tool_call.function
            if function is None:
                continue
            if function.name:
                self._tool_names[index] = function.name
            if function.arguments:
                self._arguments_by_index[index] = self._arguments_by_index.get(
                    index, ""
                ) + str(function.arguments)

            if self._tool_names.get(index) != "final_answer":
                continue

            current_text = _extract_streamed_answer_text(
                self._arguments_by_index.get(index, "")
            )
            if current_text is None:
                continue

            if current_text.startswith(self._streamed_text):
                chunk = current_text[len(self._streamed_text) :]
            else:
                chunk = current_text

            self._streamed_text = current_text
            if chunk:
                emitted.append(chunk)

        if emitted:
            return "".join(emitted)
        return None


from .display import OuroDisplay, create_logger, get_display
from .tools.agent_base import SanitizedToolCallingAgent as _SanitizedToolCallingAgent


class OuroAgent:
    def __init__(self, config: OuroAgentsConfig):
        self.config = config
        self.soul = load_soul(config.agent.workspace / "SOUL.md")
        self.notes = load_notes(config.agent.workspace / "NOTES.md")
        self.skills = load_all_skills(config)
        self.memory = create_memory_backend(config.memory)
        self._workspace = config.agent.workspace
        self._usage_tracker = UsageTracker()
        self._subagent_ledger: list[tuple[str, SubAgentUsage]] = []
        self.model = self._build_model(config.agent.model)

        self._mcp_contexts: list = []
        self._deferred_tools: dict = {}
        self._deferred_tools_by_raw_name: dict = {}
        self._deferred_index: list[dict] = []
        self._mcp_connected = False

        from .scheduler import AgentScheduler

        self.scheduler = AgentScheduler(
            config.agent.workspace / "data" / "scheduled_tasks.json"
        )

        # Load custom subagent profiles and merge into the delegatable registry
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
        context: dict = {"profile": None, "organizations": [], "teams": []}

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
        """Load MEMORY.md and today's daily log for the system prompt."""
        parts: list[str] = []
        memory_md = self._workspace / "MEMORY.md"
        if memory_md.exists():
            content = self._strip_frontmatter(memory_md.read_text()).strip()
            if content:
                parts.append(content)
        daily = self._workspace / "memory" / "daily" / f"{date.today().isoformat()}.md"
        if daily.exists():
            content = self._strip_frontmatter(daily.read_text()).strip()
            if content:
                parts.append(f"## Today's Log ({date.today().isoformat()})\n{content}")
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

    def _build_openrouter_extra_body(self, model_id: str) -> Optional[dict]:
        cfg = self.config.prompt_caching
        if not cfg.enabled or not self._is_anthropic_model(model_id):
            return None

        cache_control: dict[str, str] = {"type": "ephemeral"}
        if cfg.ttl == "1h":
            cache_control["ttl"] = "1h"
        return {"cache_control": cache_control}

    def _build_model(self, model_id: str) -> TrackedOpenAIModel:
        model_kwargs = {}
        extra_body = self._build_openrouter_extra_body(model_id)
        if extra_body:
            model_kwargs["extra_body"] = extra_body

        return TrackedOpenAIModel(
            model_id=model_id,
            api_base="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            tracker=self._usage_tracker,
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
            logger.warning("OURO_API_KEY not set — Ouro SDK unavailable in Python sandbox")
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

    def _conversation_file(self, conversation_id: str) -> Path:
        conversations_dir = self.config.agent.workspace / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        return conversations_dir / f"{conversation_id}.jsonl"

    def _append_conversation_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_summary: Optional[list[dict]] = None,
    ) -> None:
        path = self._conversation_file(conversation_id)
        entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "content": content,
        }
        if tool_summary:
            entry["tool_summary"] = tool_summary
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    @staticmethod
    def _extract_tool_summary(inner_agent, for_persistence: bool = False) -> list[dict]:
        """Extract tool call information from the inner agent's memory.

        When ``for_persistence`` is True, results are truncated for compact
        JSONL storage.  When False (default), full results are kept so they
        remain available in the current run's context window.
        """
        max_result_chars = 500 if for_persistence else 4000
        summary = []
        for step in inner_agent.memory.steps:
            if not isinstance(step, ActionStep) or not step.tool_calls:
                continue
            for tc in step.tool_calls:
                obs = step.observations or ""
                if len(obs) > max_result_chars:
                    obs = obs[:max_result_chars] + "..."
                summary.append({"tool": tc.name, "args": tc.arguments, "result": obs})
        return summary

    def _load_conversation_turns(
        self, conversation_id: str, limit: int = 24
    ) -> list[dict]:
        path = self._conversation_file(conversation_id)
        if not path.exists():
            return []

        turns: list[dict] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(json.loads(line))
                except Exception:
                    continue
        return turns[-limit:]

    @staticmethod
    def _format_turns_verbatim(turns: list[dict], max_chars: int = 1600) -> str:
        lines = []
        for turn in turns:
            role = str(turn.get("role", "unknown")).lower()
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            if len(content) > max_chars:
                content = content[:max_chars] + "..."
            lines.append(f"- {role}: {content}")
        return "\n".join(lines)

    def _summarize_turns(self, turns: list[dict]) -> str:
        """Compress older conversation turns into a brief summary."""
        condensed = []
        for turn in turns:
            role = str(turn.get("role", "unknown")).lower()
            content = str(turn.get("content", ""))[:300]
            condensed.append(f"{role}: {content}")
        blob = "\n".join(condensed)

        try:
            summary_model = self._build_model(
                self.config.heartbeat.model or self.config.agent.model
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

    def _format_conversation_turns(
        self, turns: list[dict], recent_verbatim: int = 8
    ) -> str:
        if not turns:
            return ""

        if len(turns) <= recent_verbatim:
            return self._format_turns_verbatim(turns)

        old_turns = turns[:-recent_verbatim]
        recent_turns = turns[-recent_verbatim:]

        summary = self._summarize_turns(old_turns)
        recent = self._format_turns_verbatim(recent_turns)
        return f"Earlier context: {summary}\n\nRecent:\n{recent}"

    @staticmethod
    def _compress_tool_call(tc: dict, max_result_chars: int = 600) -> str:
        """Produce a compact summary of a single tool call for history injection."""
        tool_name = tc.get("tool", "unknown")
        args = tc.get("args", {})
        result = str(tc.get("result", ""))

        # final_answer: the assistant content IS the answer, skip it
        if tool_name == "final_answer":
            return ""
        # load_tool: summarize which tools were loaded
        if tool_name == "load_tool":
            names = args.get("tool_names", [])
            if isinstance(names, list) and names:
                return f"- Loaded tools: {', '.join(str(n) for n in names)}"
            return "- Loaded tool(s)"
        # memory tools: compact summaries
        if tool_name == "memory_store":
            facts = args.get("facts", [])
            if isinstance(facts, list):
                count = len(facts)
                preview = str(facts[0].get("fact", ""))[:80] if facts else ""
                suffix = f" and {count - 1} more" if count > 1 else ""
                return f"- Stored memory: {preview}{suffix}"
            return "- Stored memory"
        if tool_name == "memory_recall":
            queries = args.get("queries", [])
            if isinstance(queries, list):
                query_strs = [
                    str(q.get("query", q) if isinstance(q, dict) else q)[:50]
                    for q in queries[:3]
                ]
                count = result.count("\n- ") + (1 if result.startswith("- ") else 0)
                return f"- Recalled {count} memories for: {'; '.join(query_strs)}"
            return "- Recalled memories"
        # Default: name + args + result (keep more for recent history)
        result_preview = result[:max_result_chars]
        if len(result) > max_result_chars:
            result_preview += "..."
        return f"- {tool_name}({json.dumps(args)}) → {result_preview}"

    @staticmethod
    def _build_history_steps(turns: list[dict]) -> list:
        """Convert JSONL conversation turns into smolagents memory steps.

        Pairs user/assistant turns into TaskStep + ActionStep sequences so the
        model sees proper structured conversation history instead of a text blob.
        """
        _DUMMY_TIMING = Timing(start_time=0.0, end_time=0.0)
        steps: list = []
        i = 0
        while i < len(turns):
            turn = turns[i]
            role = turn.get("role", "")
            content = turn.get("content", "")

            if role == "user":
                steps.append(TaskStep(task=content))
                # Look for a paired assistant response
                if i + 1 < len(turns) and turns[i + 1].get("role") == "assistant":
                    assistant_turn = turns[i + 1]
                    assistant_content = assistant_turn.get("content", "")
                    tool_summary = assistant_turn.get("tool_summary")

                    model_output = assistant_content
                    if tool_summary:
                        tool_lines = [
                            OuroAgent._compress_tool_call(tc) for tc in tool_summary
                        ]
                        tool_lines = [tl for tl in tool_lines if tl]
                        if tool_lines:
                            model_output = (
                                "Tools used:\n"
                                + "\n".join(tool_lines)
                                + "\n\n"
                                + assistant_content
                            )

                    steps.append(
                        ActionStep(
                            step_number=len(steps),
                            timing=_DUMMY_TIMING,
                            model_output=model_output,
                            is_final_answer=True,
                        )
                    )
                    i += 2
                    continue
            elif role == "assistant":
                steps.append(
                    ActionStep(
                        step_number=len(steps),
                        timing=_DUMMY_TIMING,
                        model_output=content,
                        is_final_answer=True,
                    )
                )
            i += 1
        return steps

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
        mode: RunMode = RunMode.AUTONOMOUS,
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

        if mode == RunMode.HEARTBEAT:
            servers = set(allowed_servers) if allowed_servers else {"ouro"}
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
        )
        ouro_client = self._get_ouro_client()
        python_tool, _executor = make_python_tool(
            workspace=self.config.agent.workspace,
            ouro_client=ouro_client,
        )
        scheduler_tools = (
            make_scheduler_tools(self.scheduler) if mode != RunMode.HEARTBEAT else []
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
                    result, profile, sa, spec.get("return_mode", ""),
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

        delegate_tools = (
            [delegate] if mode != RunMode.HEARTBEAT else []
        )
        all_tools = (
            list(memory_tools)
            + scheduler_tools
            + delegate_tools
            + [load_tool, python_tool]
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

        deferred_tool_directory = "\n".join(
            f"- {item['tool']}: {item['description'][:240]}" for item in deferred_index
        )

        return all_tools, deferred_tool_directory, agent_ref, preloaded_names

    def _build_system_prompt(
        self,
        task: str,
        mode: RunMode,
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
        if mode == RunMode.CHAT and conversation_state:
            conversation_state_text = conversation_state.format_for_prompt()
        elif conversation_id and mode != RunMode.HEARTBEAT:
            turns = self._load_conversation_turns(conversation_id, limit=24)
            conversation_context = self._format_conversation_turns(turns)

        skills_text = "" if mode == RunMode.HEARTBEAT else self.skills

        working_memory_parts = [self._load_working_memory()]
        if mode == RunMode.HEARTBEAT:
            working_memory_parts.append(self._load_scheduled_task_awareness())
        working_memory = "\n\n".join(part for part in working_memory_parts if part)

        user_model_text = ""
        if user_id:
            user_model_text = load_user_model(self.config.agent.workspace, user_id)

        from .memory.context_loader import load_entity_context

        entity_context_text = load_entity_context(
            self.config.agent.workspace,
            conversation_state=conversation_state,
            task=task,
        )

        # Build the subagent directory for the prompt
        from .subagents.profiles import DELEGATABLE_PROFILES

        if mode != RunMode.HEARTBEAT and DELEGATABLE_PROFILES:
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
            working_memory=working_memory,
            mode=mode,
            conversation_context=conversation_context,
            conversation_state=conversation_state_text,
            user_model=user_model_text,
            entity_context=entity_context_text,
            deferred_tool_directory=deferred_tool_directory,
            subagent_directory=subagent_directory,
            mode_framing_override=mode_framing_override,
            platform_context=self._load_platform_context(),
            chat_conversation_id=conversation_id if mode == RunMode.CHAT else None,
            preloaded_tool_names=preloaded_tool_names,
        )

    def _resolve_subagent_model(self, profile) -> "TrackedOpenAIModel":
        """Resolve the model for a subagent profile using the override cascade."""
        override = self.config.subagents.overrides.get(profile.name)
        model_id = (
            profile.model_override
            or (override.model if override else None)
            or self.config.subagents.default_model
            or self.config.agent.model
        )
        return self._build_model(model_id)

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
    ) -> "SubAgentContext":
        from .subagents.context import SubAgentContext

        compactor_model = self._build_model(
            self.config.heartbeat.model or self.config.agent.model
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

        model = self._resolve_subagent_model(profile)
        effective_profile = self._apply_profile_overrides(profile)

        ctx = self._build_subagent_context(
            effective_profile,
            model,
            task=task,
            conversation_state=conversation_state,
            conversation_id=conversation_id,
            user_id=user_id,
            run_id=run_id,
            asset_refs=asset_refs,
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

            model = self._resolve_subagent_model(profile)
            effective_profile = self._apply_profile_overrides(profile)

            ctx = self._build_subagent_context(
                effective_profile,
                model,
                task=task_str,
                conversation_state=conversation_state,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
                asset_refs=extra.get("asset_refs"),
            )
            dispatch_list.append((effective_profile, task_str, ctx))

        return run_subagents_parallel(dispatch_list)

    @staticmethod
    def _tool_activity_message(tool_name: str) -> str:
        if tool_name == "load_tool":
            return "is preparing a tool"
        if tool_name == "delegate":
            return "is delegating to a subagent"
        if tool_name.startswith("memory_"):
            return "is checking memory"
        if tool_name in ("python_interpreter", "run_python"):
            return "is running Python"
        return f"is using {tool_name}"

    def _build_step_callback(
        self,
        status_callback: Optional[RunStatusCallback],
        display: Optional[OuroDisplay] = None,
    ) -> Callable[[ActionStep], None]:
        last_message: dict[str, Optional[str]] = {"value": None}
        tracker = self._usage_tracker
        _display = display or get_display()

        def _emit(message: str) -> None:
            _display.step(message)
            if not status_callback:
                return
            if last_message["value"] == message:
                return
            last_message["value"] = message
            try:
                status_callback("thinking", message, True)
            except Exception:
                logger.exception("Failed to emit activity update")

        def _callback(step: ActionStep) -> None:
            in_tok = tracker.total_input_tokens
            out_tok = tracker.total_output_tokens
            step_num = getattr(step, "step_number", 0)
            timing = getattr(step, "timing", None)
            duration_s = None
            if timing is not None:
                start_time = getattr(timing, "start_time", None)
                end_time = getattr(timing, "end_time", None)
                if isinstance(start_time, (int, float)) and isinstance(
                    end_time, (int, float)
                ):
                    duration_s = max(0.0, end_time - start_time)

            logger.info(
                "[Step %d] Tokens so far: in=%s out=%s total=%s",
                step_num,
                f"{in_tok:,}",
                f"{out_tok:,}",
                f"{in_tok + out_tok:,}",
            )
            cost = getattr(tracker, "total_cost_usd", None)
            _display.token_summary(
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_input_tokens=tracker.total_cached_input_tokens,
                step_number=step_num,
                duration_s=duration_s,
                cost_usd=cost,
            )

            if getattr(step, "is_final_answer", False):
                return
            if step.error:
                _emit("hit an error, retrying...")
                return
            tool_calls = getattr(step, "tool_calls", None) or []
            if tool_calls:
                tool_name = getattr(tool_calls[0], "name", None) or "a tool"
                _display.tool_call(tool_name)
                if status_callback:
                    msg = self._tool_activity_message(tool_name)
                    if last_message["value"] != msg:
                        last_message["value"] = msg
                        try:
                            status_callback("thinking", msg, True)
                        except Exception:
                            logger.exception("Failed to emit activity update")
                return
            _emit("thinking...")

        return _callback

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
        _display.step("step 0: analyzing task...")
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
            from .memory.reflection import parse_reflection_result
            from .subagents.profiles import REFLECTOR

            reflection_output = self._run_subagent(
                REFLECTOR,
                "reflect",
                conversation_state=conv_state,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
            )
            if not reflection_output.success:
                logger.warning("Reflector subagent failed: %s", reflection_output.error)
                return
            reflection_result = parse_reflection_result(reflection_output.text)
            apply_reflection(
                reflection_result,
                self.memory,
                agent_id=self.config.agent.name,
                user_id=user_id,
                conversation_id=conversation_id,
                workspace=self.config.agent.workspace,
                conversations_dir=conversations_dir,
                conversation_state=conv_state,
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

    async def run(
        self,
        task: str,
        model_override=None,
        conversation_id: Optional[str] = None,
        mode: RunMode = RunMode.AUTONOMOUS,
        user_id: Optional[str] = None,
        status_callback: Optional[RunStatusCallback] = None,
        response_callback: Optional[RunResponseCallback] = None,
        skip_memory: bool = False,
        allowed_servers: Optional[list[str]] = None,
        mode_framing_override: str = "",
        preload_tools: Optional[list[str]] = None,
        asset_refs: Optional[list[str]] = None,
    ) -> str:
        run_started_at = time.monotonic()
        self.connect_mcp()
        model = model_override or self.model

        self._usage_tracker.reset()
        self._subagent_ledger.clear()
        run_id = conversation_id or f"run_{uuid4().hex[:12]}"

        # Merge mode-specific preload tools with any explicit preload_tools
        mode_preloads = {
            RunMode.CHAT: self.config.agent.preload_tools.chat,
            RunMode.AUTONOMOUS: self.config.agent.preload_tools.autonomous,
            RunMode.HEARTBEAT: self.config.agent.preload_tools.heartbeat,
        }.get(mode, [])
        if mode_preloads:
            preload_tools = list(set((preload_tools or []) + mode_preloads))

        # --- Conversation state (chat mode) ---
        conv_state: Optional[ConversationState] = None
        if mode == RunMode.CHAT and conversation_id:
            conversations_dir = self.config.agent.workspace / "conversations"
            conv_state = load_state(conversations_dir, conversation_id)

        # --- Trivial message fast path (regex only, no LLM) ---
        is_trivial = is_trivial_message(task)

        # --- Build tools (full set, no classification filtering) ---
        all_tools, deferred_tool_directory, agent_ref, preloaded_names = (
            self._build_agent_tools(
                mode,
                user_id=user_id,
                allowed_servers=allowed_servers,
                preload_tools=preload_tools,
                conversation_state=conv_state,
                conversation_id=conversation_id,
                run_id=run_id,
            )
        )

        # Build system prompt (static, cacheable) + dynamic context (per-turn).
        system_prompt, dynamic_context = self._build_system_prompt(
            task=task,
            mode=mode,
            conversation_id=conversation_id if mode != RunMode.CHAT else None,
            deferred_tool_directory=deferred_tool_directory,
            user_id=user_id,
            conversation_state=conv_state,
            mode_framing_override=mode_framing_override,
            preloaded_tool_names=preloaded_names,
        )

        # --- Step 0: Preflight subagent (visible) ---
        # Replaces the old hidden classifier, context_loader, planner, and
        # retrieve_memories pipeline with one visible subagent call.
        display = get_display()
        preflight: Optional[PreflightResult] = None

        if not is_trivial and not skip_memory and mode != RunMode.HEARTBEAT:
            preflight = self._run_preflight(
                task,
                conv_state=conv_state,
                user_id=user_id,
                run_id=run_id,
                asset_refs=asset_refs,
                display=display,
                status_callback=status_callback,
            )
            logger.info(
                "Preflight: intent=%s complexity=%s worth_remembering=%s briefing=%d plan=%d",
                preflight.intent,
                preflight.complexity,
                preflight.worth_remembering,
                len(preflight.briefing),
                len(preflight.plan),
            )

        # Assemble the effective task: dynamic context + preflight briefing/plan + request
        context_parts: list[str] = []
        if dynamic_context:
            context_parts.append(dynamic_context)
        asset_context = fetch_asset_content(self._deferred_tools, list(asset_refs or []))
        if asset_context:
            context_parts.append(f"## Input Assets\n{asset_context}")
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

        step_callback = self._build_step_callback(status_callback, display)
        main_max_steps = {
            RunMode.CHAT: self.config.agent.max_steps.chat,
            RunMode.AUTONOMOUS: self.config.agent.max_steps.autonomous,
            RunMode.HEARTBEAT: self.config.agent.max_steps.heartbeat,
        }[mode]
        compactor_model = self._build_model(
            self.config.heartbeat.model or self.config.agent.model
        )
        agent = _SanitizedToolCallingAgent(
            tools=all_tools,
            model=model,
            max_steps=main_max_steps,
            stream_outputs=bool(response_callback),
            step_callbacks=[step_callback],
            logger=create_logger(display=display),
            compactor_model=compactor_model,
        )
        agent_ref["agent"] = agent

        agent.prompt_templates["system_prompt"] = (
            agent.prompt_templates["system_prompt"] + "\n\n" + system_prompt
        )

        # In chat mode, inject recent turns as structured steps so the model
        # sees user/assistant pairs verbatim.
        has_history = False
        if mode == RunMode.CHAT and conversation_id:
            turns = self._load_conversation_turns(conversation_id, limit=8)
            if turns:
                history_steps = self._build_history_steps(turns)
                agent.memory.steps.extend(history_steps)
                has_history = True
                logger.info(
                    "Injected %d history steps from %d recent turns for conversation %s",
                    len(history_steps),
                    len(turns),
                    conversation_id,
                )

        use_reset = not has_history

        if response_callback:
            final_result = None
            streamer = _FinalAnswerStreamer()
            for event in agent.run(effective_task, stream=True, reset=use_reset):
                if isinstance(event, ChatMessageStreamDelta):
                    chunk = streamer.consume(event)
                    if chunk:
                        response_callback(chunk)
                elif isinstance(event, FinalAnswerStep):
                    final_result = event.output
            result = final_result if final_result is not None else ""
        else:
            result = agent.run(effective_task, reset=use_reset)

        tool_summary = self._extract_tool_summary(agent, for_persistence=True)

        # Memory storage: skip for trivial messages and chat mode (reflector handles it)
        worth_remembering = preflight.worth_remembering if preflight else not is_trivial
        should_store = mode != RunMode.CHAT and not skip_memory and worth_remembering
        if should_store:
            self.memory.add(
                [
                    {"role": "user", "content": task},
                    {"role": "assistant", "content": str(result)},
                ],
                agent_id=self.config.agent.name,
                user_id=user_id,
                run_id=conversation_id,
            )
        if conversation_id and mode != RunMode.HEARTBEAT:
            self._append_conversation_turn(conversation_id, "user", task)
            self._append_conversation_turn(
                conversation_id,
                "assistant",
                str(result),
                tool_summary=tool_summary or None,
            )

        # Post-turn: update conversation state + mid-session reflection
        if mode == RunMode.CHAT and conversation_id:
            try:
                state_model = self._build_model(
                    self.config.heartbeat.model or self.config.agent.model
                )
                conv_state = update_state(conv_state, task, str(result), state_model)
                conversations_dir = self.config.agent.workspace / "conversations"
                save_state(conversations_dir, conversation_id, conv_state)
                logger.info(
                    "Updated conversation state for %s: topic=%s, turn=%d",
                    conversation_id,
                    conv_state.current_topic,
                    conv_state.turn_count,
                )
            except Exception as e:
                logger.warning("Failed to update conversation state: %s", e)

            # Mid-session reflection (post-turn): curate memories after responding
            self._maybe_reflect_post_turn(
                conv_state=conv_state,
                conversation_id=conversation_id,
                user_id=user_id,
                run_id=run_id,
            )

        usage = collect_run_usage(agent, model, self._usage_tracker)
        logger.info(
            "Run usage:\n%s",
            format_usage_breakdown(usage, self._subagent_ledger),
        )
        _display = display or get_display()
        ledger = self._subagent_ledger or None
        _display.run_summary(
            usage=usage,
            duration_s=max(0.0, time.monotonic() - run_started_at),
            subagent_ledger=ledger,
        )

        self._log_run(
            task,
            result,
            model.model_id if hasattr(model, "model_id") else str(model),
            mode,
            usage=usage,
        )

        return str(result)

    async def heartbeat(self) -> Optional[str]:
        hb_model_id = self.config.heartbeat.model or self.config.agent.model
        hb_model = self._build_model(hb_model_id)

        try:
            self._refresh_platform_context()
        except Exception as e:
            logger.warning("Failed to refresh platform context during heartbeat: %s", e)

        # Run memory consolidation on each heartbeat
        if self.config.memory.consolidation_enabled:
            from .memory.consolidation import run_consolidation

            try:
                run_consolidation(
                    workspace=self.config.agent.workspace,
                    backend=self.memory,
                    agent_id=self.config.agent.name,
                    config=self.config.memory,
                    model=hb_model,
                )
            except Exception as e:
                logger.warning("Memory consolidation failed during heartbeat: %s", e)

        proactive_cfg = self.config.heartbeat.proactive
        servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

        # --- Planning cycle integration ---
        if self.config.planning.enabled:
            from .planning import PlanStore, next_action

            plan_store = PlanStore(self.config.agent.workspace / "plans")
            current = plan_store.load_current()
            planning_cfg = self.config.planning

            action = next_action(
                current=current,
                cadence=planning_cfg.cadence,
                min_heartbeats=planning_cfg.min_heartbeats,
                review_window=planning_cfg.review_window,
                auto_approve=planning_cfg.auto_approve,
            )

            if action == "plan":
                if current and current.status == "active":
                    plan_store.archive_current()
                return await self._planning_heartbeat(hb_model, plan_store, servers)

            if action == "check_review":
                reviewed = await self._review_heartbeat(
                    hb_model, plan_store, current, servers
                )
                if reviewed:
                    current = reviewed

            if action == "execute" and current and current.status == "pending_review":
                # Auto-approve: review window elapsed, activate the plan as-is
                current.status = "active"
                current.activated_at = datetime.now(timezone.utc).isoformat()
                plan_store.save_current(current)
                logger.info(
                    "Plan cycle %s auto-approved (review window elapsed)", current.id
                )
                write_daily_log(
                    self.config.agent.workspace,
                    "[planning:auto-approved] Plan activated without feedback",
                )

            if current and current.status == "active":
                current.heartbeats_completed += 1
                plan_store.save_current(current)

        # Load the autonomous playbook
        heartbeat_path = self.config.agent.workspace / "HEARTBEAT.md"
        if not heartbeat_path.exists():
            return None
        playbook = heartbeat_path.read_text()

        from .heartbeat import is_within_active_hours

        if not is_within_active_hours(self.config.heartbeat):
            playbook += (
                "\n\n**Note: You are outside active hours. "
                "Only check notifications unless something is urgent.**"
            )

        # Inject active plan as context for the execution heartbeat
        if self.config.planning.enabled:
            current = plan_store.load_current()
            if current and current.status == "active" and current.plan_text:
                playbook += f"\n\n## Current Plan\nYou are executing the following plan:\n{current.plan_text}"

        result = await self.run(
            playbook,
            model_override=hb_model,
            mode=RunMode.HEARTBEAT,
            allowed_servers=servers,
        )

        # Parse structured JSON response and log to daily memory
        try:
            json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(1))
            else:
                parsed = json.loads(result)

            if parsed.get("action") == "none":
                logger.info("Heartbeat: no action taken")
                return None

            action = parsed.get("action", "unknown")
            details = parsed.get("details", "")
            logger.info("Heartbeat action: %s", action)

            log_entry = (
                f"[heartbeat:{action}] {details}"
                if details
                else f"[heartbeat:{action}]"
            )
            write_daily_log(self.config.agent.workspace, log_entry)
        except json.JSONDecodeError:
            pass

        return result

    async def _planning_heartbeat(
        self, hb_model, plan_store, servers: list[str]
    ) -> Optional[str]:
        """Run a planning heartbeat: generate a plan and publish it for review."""
        from .planning import PlanCycle, build_planning_prompt
        from .soul import PLANNING_FRAMING

        planning_cfg = self.config.planning
        previous = plan_store.load_history(limit=1)
        previous_plan = previous[0] if previous else None

        prompt = build_planning_prompt(
            cadence=planning_cfg.cadence,
            team_id=planning_cfg.team_id,
            org_id=planning_cfg.org_id,
            previous_plan=previous_plan,
        )

        result = await self.run(
            prompt,
            model_override=hb_model,
            mode=RunMode.HEARTBEAT,
            allowed_servers=servers,
            mode_framing_override=PLANNING_FRAMING,
            preload_tools=["ouro:create_post"],
        )

        # Parse the structured JSON response to extract plan + post_id
        cycle = PlanCycle(status="pending_review")
        try:
            json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
            raw = json_match.group(1) if json_match else result
            parsed = json.loads(raw)
            cycle.plan_text = parsed.get("plan", "")
            cycle.post_id = parsed.get("post_id")
        except (json.JSONDecodeError, AttributeError):
            logger.warning(
                "Could not parse planning result as JSON, storing raw result"
            )
            cycle.plan_text = result

        plan_store.save_current(cycle)
        logger.info("Planning cycle %s created (post_id=%s)", cycle.id, cycle.post_id)
        write_daily_log(
            self.config.agent.workspace,
            f"[planning:created] New plan cycle {cycle.id[:8]}",
        )
        return result

    async def _review_heartbeat(
        self,
        hb_model,
        plan_store,
        current,
        servers: list[str],
        inline_feedback: Optional[str] = None,
    ):
        """Check for human feedback on the plan post and activate if reviewed.

        If *inline_feedback* is provided (e.g. from a webhook event), it is
        included directly in the prompt so the agent doesn't need to call
        get_comments.
        """
        from .planning import build_feedback_review_prompt, build_review_prompt
        from .soul import REVIEW_FRAMING

        if not current or not current.post_id:
            # No post to check — auto-activate
            if current:
                current.status = "active"
                current.activated_at = datetime.now(timezone.utc).isoformat()
                plan_store.save_current(current)
                logger.info("Plan cycle %s activated (no post to review)", current.id)
                write_daily_log(
                    self.config.agent.workspace,
                    "[planning:activated] Plan activated (no post)",
                )
            return current

        if inline_feedback:
            prompt = build_feedback_review_prompt(
                post_id=current.post_id,
                plan_text=current.plan_text,
                feedback_text=inline_feedback,
            )
            preload = ["ouro:update_post", "ouro:create_comment"]
        else:
            prompt = build_review_prompt(
                post_id=current.post_id, plan_text=current.plan_text
            )
            preload = ["ouro:get_comments", "ouro:update_post"]

        result = await self.run(
            prompt,
            model_override=hb_model,
            mode=RunMode.HEARTBEAT,
            allowed_servers=servers,
            mode_framing_override=REVIEW_FRAMING,
            preload_tools=preload,
        )

        # Parse the review result
        try:
            json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
            raw = json_match.group(1) if json_match else result
            parsed = json.loads(raw)
            feedback = parsed.get("feedback_summary")
            revised = parsed.get("revised_plan")

            if feedback:
                current.human_feedback = feedback
                current.plan_text = revised or current.plan_text
                current.status = "active"
                current.activated_at = datetime.now(timezone.utc).isoformat()
                plan_store.save_current(current)
                logger.info("Plan cycle %s activated with feedback", current.id)
                write_daily_log(
                    self.config.agent.workspace,
                    f"[planning:reviewed] Plan updated with feedback: {feedback[:100]}",
                )
                return current
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Could not parse review result as JSON")

        # No feedback found — leave as pending_review
        return None

    async def force_planning_heartbeat(self) -> Optional[str]:
        """Force a planning cycle regardless of cadence/timing (CLI entry point)."""
        from .planning import PlanStore

        hb_model_id = self.config.heartbeat.model or self.config.agent.model
        hb_model = self._build_model(hb_model_id)

        try:
            self._refresh_platform_context()
        except Exception as e:
            logger.warning("Failed to refresh platform context: %s", e)

        if self.config.memory.consolidation_enabled:
            from .memory.consolidation import run_consolidation

            try:
                run_consolidation(
                    workspace=self.config.agent.workspace,
                    backend=self.memory,
                    agent_id=self.config.agent.name,
                    config=self.config.memory,
                    model=hb_model,
                )
            except Exception as e:
                logger.warning("Memory consolidation failed: %s", e)

        proactive_cfg = self.config.heartbeat.proactive
        servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

        plan_store = PlanStore(self.config.agent.workspace / "plans")
        current = plan_store.load_current()
        if current and current.status == "active":
            plan_store.archive_current()

        return await self._planning_heartbeat(hb_model, plan_store, servers)

    async def force_review_heartbeat(self) -> Optional[str]:
        """Force a review check on the current plan (CLI entry point)."""
        from .planning import PlanStore

        plan_store = PlanStore(self.config.agent.workspace / "plans")
        current = plan_store.load_current()

        if not current or current.status not in ("pending_review", "active"):
            logger.info("No plan cycle to review")
            return None

        hb_model_id = self.config.heartbeat.model or self.config.agent.model
        hb_model = self._build_model(hb_model_id)

        try:
            self._refresh_platform_context()
        except Exception as e:
            logger.warning("Failed to refresh platform context: %s", e)

        proactive_cfg = self.config.heartbeat.proactive
        servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

        reviewed = await self._review_heartbeat(hb_model, plan_store, current, servers)
        if reviewed:
            return f"Plan activated.\n\n{reviewed.plan_text}"
        return "No feedback found — plan remains pending review."

    async def handle_plan_feedback(self, event_run) -> Optional[str]:
        """Handle feedback on a plan post from an incoming event.

        For active/pending_review plans, runs the review heartbeat with the
        feedback text inlined.  For completed plans, runs the event task
        normally (the enriched task framing tells the agent to store insights).
        """
        from .planning import PlanStore

        prov = event_run.provenance
        if not prov or not prov.plan_cycle:
            return None

        plan_store = PlanStore(self.config.agent.workspace / "plans")
        current = plan_store.load_current()

        if (
            current
            and current.post_id == prov.plan_cycle.post_id
            and current.status in ("pending_review", "active")
        ):
            hb_model_id = self.config.heartbeat.model or self.config.agent.model
            hb_model = self._build_model(hb_model_id)
            proactive_cfg = self.config.heartbeat.proactive
            servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

            reviewed = await self._review_heartbeat(
                hb_model,
                plan_store,
                current,
                servers,
                inline_feedback=event_run.task,
            )
            if reviewed:
                logger.info("Plan updated via event feedback (cycle %s)", reviewed.id)
            return reviewed.plan_text if reviewed else None

        # Historical or unmatched — run as a normal enriched task
        return await self.run(
            task=event_run.task,
            mode=event_run.mode,
            user_id=event_run.user_id,
            preload_tools=list(event_run.preload_tools) if event_run.preload_tools else None,
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
