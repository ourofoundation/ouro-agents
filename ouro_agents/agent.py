import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from smolagents import (
    ActionStep,
    ChatMessageStreamDelta,
    FinalAnswerStep,
    ToolCallingAgent,
    ToolCollection,
    tool,
)
from smolagents.memory import TaskStep
from smolagents.monitoring import Timing

from .classify import TaskClassification, classify_task
from .config import MCPServerConfig, OuroAgentsConfig, RunMode
from .memory import create_memory_backend
from .memory.retrieval import retrieve_memories
from .memory.conversation_state import (
    ConversationState,
    load_state,
    save_state,
    update_state,
)
from .memory.reflection import (
    apply_reflection,
    reflect,
    should_reflect_for_conversation,
    write_daily_log,
)
from .memory.tools import make_memory_tools
from .memory.user_model import load_user_model
from .notes import load_notes
from .skills import load_all_skills, load_relevant_skills
from .soul import build_prompt, load_soul
from .tools.python_tool import make_python_tool
from .tools.scheduler_tools import make_scheduler_tools
from .usage import (
    RunUsage,
    TrackedOpenAIModel,
    UsageTracker,
    collect_run_usage,
    format_usage_summary,
)

logger = logging.getLogger(__name__)

_NULL_STRINGS = {"null", "None", "none", "undefined"}
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
                self._arguments_by_index[index] = (
                    self._arguments_by_index.get(index, "") + str(function.arguments)
                )

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


class _SanitizedToolCallingAgent(ToolCallingAgent):
    """ToolCallingAgent with automatic null-string cleanup.

    LLMs (especially smaller ones) frequently emit the literal string "null"
    for optional parameters instead of omitting them.  smolagents' validation
    then rejects the value with a type-mismatch error, burning steps.

    This subclass strips those bogus values before validation so the call
    goes through on the first attempt.
    """

    def execute_tool_call(self, tool_name, arguments):
        if isinstance(arguments, dict):
            available_tools = {**self.tools, **self.managed_agents}
            tool_obj = available_tools.get(tool_name)
            if tool_obj and hasattr(tool_obj, "inputs"):
                cleaned = {}
                for key, value in arguments.items():
                    if key not in tool_obj.inputs:
                        cleaned[key] = value
                        continue
                    schema = tool_obj.inputs[key]
                    is_nullable = schema.get("nullable", False)
                    expected_type = schema.get("type", "any")
                    # Drop string-encoded nulls for nullable non-string params
                    if (
                        is_nullable
                        and isinstance(value, str)
                        and value in _NULL_STRINGS
                        and expected_type != "string"
                    ):
                        continue
                    # Drop actual None for nullable params
                    if is_nullable and value is None:
                        continue
                    cleaned[key] = value
                arguments = cleaned
        return super().execute_tool_call(tool_name, arguments)


class OuroAgent:
    def __init__(self, config: OuroAgentsConfig):
        self.config = config
        self.soul = load_soul(config.agent.workspace / "SOUL.md")
        self.notes = load_notes(config.agent.workspace / "NOTES.md")
        self.skills = load_all_skills(config)
        self.memory = create_memory_backend(config.memory)
        self._workspace = config.agent.workspace
        self._usage_tracker = UsageTracker()
        self.model = self._build_model(config.agent.model)

        self._mcp_contexts: list = []
        self._deferred_tools: dict = {}
        self._deferred_tools_by_raw_name: dict = {}
        self._deferred_index: list[dict] = []
        self._mcp_connected = False

        from .scheduler import AgentScheduler
        self.scheduler = AgentScheduler(config.agent.workspace / "data" / "scheduled_tasks.json")

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """Remove YAML frontmatter (---...---) from markdown text."""
        if not text.startswith("---"):
            return text
        end = text.find("---", 3)
        if end == -1:
            return text
        return text[end + 3:].lstrip("\n")

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
                context["organizations"] = data.get("results", data) if isinstance(data, dict) else data
            except Exception as e:
                logger.warning("Platform context: failed to fetch orgs: %s", e)

        teams_tool = self._deferred_tools.get("ouro:get_teams")
        if teams_tool:
            try:
                raw = teams_tool()
                data = json.loads(raw) if isinstance(raw, str) else raw
                context["teams"] = data.get("results", data) if isinstance(data, dict) else data
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
        cache_path = self._workspace / "data" / "platform_context.json"
        if not cache_path.exists():
            return ""

        try:
            context = json.loads(cache_path.read_text())
        except Exception:
            return ""

        parts: list[str] = []

        profile = context.get("profile")
        if profile:
            username = profile.get("username", "?")
            display = profile.get("display_name")
            name_str = f"{display} (@{username})" if display else f"@{username}"
            parts.append(
                f"You are: {name_str} (id: {profile.get('id', '?')}, "
                f"email: {profile.get('email', '?')})"
            )

        orgs = context.get("organizations", [])
        if orgs:
            parts.append("\nYour organizations:")
            for org in orgs:
                display = org.get("display_name") or org.get("name", "unknown")
                parts.append(
                    f"- {display} (id: {org.get('id', '?')}, role: {org.get('role', '?')})"
                )

        teams = context.get("teams", [])
        if teams:
            parts.append("\nYour teams:")
            for team in teams:
                desc = team.get("description", "")
                line = (
                    f"- {team.get('name', '?')} "
                    f"(id: {team.get('id', '?')}, "
                    f"org: {team.get('organization_name', '?')}, "
                    f"role: {team.get('role', '?')})"
                )
                if desc:
                    line += f" — {desc}"
                parts.append(line)

        if not parts:
            return ""
        parts.append(
            "\nUse these IDs directly — no need to call get_organizations or get_teams "
            "unless you need to discover new teams or refresh membership info."
        )
        return "\n".join(parts)

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
    def _extract_tool_summary(inner_agent) -> list[dict]:
        """Extract a compact summary of tool calls from the inner agent's memory."""
        summary = []
        for step in inner_agent.memory.steps:
            if not isinstance(step, ActionStep) or not step.tool_calls:
                continue
            for tc in step.tool_calls:
                obs = step.observations or ""
                if len(obs) > 300:
                    obs = obs[:300] + "..."
                summary.append({"tool": tc.name, "args": tc.arguments, "result": obs})
        return summary

    def _load_conversation_turns(
        self, conversation_id: str, limit: int = 12
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
    def _format_turns_verbatim(turns: list[dict], max_chars: int = 800) -> str:
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
        self, turns: list[dict], recent_verbatim: int = 4
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
    def _compress_tool_call(tc: dict) -> str:
        """Produce a compact one-line summary of a single tool call."""
        tool_name = tc.get("tool", "unknown")
        args = tc.get("args", {})
        result = str(tc.get("result", ""))

        # final_answer: the assistant content IS the answer, skip it
        if tool_name == "final_answer":
            return ""
        # load_tool: only the tool name matters
        if tool_name == "load_tool":
            loaded = args.get("tool_name", "?")
            return f"- Loaded tool: {loaded}"
        # memory tools: compact summaries
        if tool_name == "memory_store":
            fact = str(args.get("fact", ""))[:100]
            return f"- Stored memory: {fact}"
        if tool_name == "memory_recall":
            query = str(args.get("query", ""))[:60]
            count = result.count("\n- ") + (1 if result.startswith("- ") else 0)
            return f"- Recalled {count} memories for: {query}"
        # Default: name + args + truncated result
        result_preview = result[:300]
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
                            OuroAgent._compress_tool_call(tc)
                            for tc in tool_summary
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

                server_params = StdioServerParameters(
                    command=server.command, args=server.args or [], env=server.env
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
            pass

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
        classification: Optional[TaskClassification] = None,
        allowed_servers: Optional[list[str]] = None,
        preload_tools: Optional[list[str]] = None,
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
        elif classification and classification.relevant_servers:
            always_available = {"search"}
            servers = set(classification.relevant_servers) | always_available
            deferred_index = [
                item
                for item in self._deferred_index
                if item["server"] in servers
            ]
            relevant_names = {item["tool"] for item in deferred_index}
            deferred_tools = {
                k: v for k, v in self._deferred_tools.items() if k in relevant_names
            }

        agent_self = self
        agent_ref: dict = {}

        @tool
        def load_tool(tool_name: str) -> str:
            """Load a deferred MCP tool so you can call it directly by name.
            Args:
                tool_name: Tool name from the deferred tool directory (e.g. ouro:search_assets)
            """
            resolved_name, err = agent_self._resolve_tool_name(tool_name)
            if err:
                top_examples = [item["tool"] for item in deferred_index[:8]]
                return json.dumps(
                    {
                        "error": err,
                        "example_tools": top_examples,
                        "hint": "Pick from the deferred tool directory in system context.",
                    }
                )

            item = next(i for i in deferred_index if i["tool"] == resolved_name)
            target = deferred_tools.get(resolved_name)
            if not target:
                return json.dumps({"error": f"Tool '{resolved_name}' not available."})

            raw_name = item["raw_name"]

            running_agent = agent_ref.get("agent")
            if running_agent is not None:
                running_agent.tools[raw_name] = target

            return json.dumps(
                {
                    "status": "loaded",
                    "call_as": raw_name,
                    "description": item["description"],
                    "inputs": item["inputs"],
                    "output_type": item["output_type"],
                }
            )

        memory_tools = make_memory_tools(
            self.memory, self.config.agent.name,
            user_id=user_id, workspace=self.config.agent.workspace,
        )
        python_tool, _executor = make_python_tool(workspace=self.config.agent.workspace)
        scheduler_tools = make_scheduler_tools(self.scheduler) if mode != RunMode.HEARTBEAT else []
        all_tools = list(memory_tools) + scheduler_tools + [load_tool, python_tool]

        preloaded_names: list[str] = []
        for qualified_name in preload_tools or []:
            resolved, err = self._resolve_tool_name(qualified_name)
            if err or not resolved:
                logger.warning("Preload skipped for '%s': %s", qualified_name, err)
                continue
            target = deferred_tools.get(resolved)
            if not target:
                logger.warning("Preload skipped for '%s': not in available deferred tools", resolved)
                continue
            item = next(
                (i for i in deferred_index if i["tool"] == resolved), None
            )
            if item:
                all_tools.append(target)
                preloaded_names.append(item["raw_name"])
                logger.info("Preloaded tool: %s (call as %s)", resolved, item["raw_name"])

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
        classification: Optional[TaskClassification] = None,
        conversation_state: Optional[ConversationState] = None,
        mode_framing_override: str = "",
        preloaded_tool_names: Optional[list[str]] = None,
    ) -> str:
        # In chat mode, conversation state replaces the raw history summary
        # in the system prompt.  Raw history is injected as structured steps
        # instead (see run() method).
        conversation_context = ""
        conversation_state_text = ""
        if mode == RunMode.CHAT and conversation_state:
            conversation_state_text = conversation_state.format_for_prompt()
        elif conversation_id and mode != RunMode.HEARTBEAT:
            turns = self._load_conversation_turns(conversation_id, limit=12)
            conversation_context = self._format_conversation_turns(turns)

        if mode == RunMode.HEARTBEAT:
            skills_text = ""
        elif classification and classification.relevant_skills:
            skills_text = load_relevant_skills(
                self.config, classification.relevant_skills
            )
        else:
            skills_text = self.skills

        working_memory_parts = [self._load_working_memory()]
        if mode == RunMode.HEARTBEAT:
            working_memory_parts.append(self._load_scheduled_task_awareness())
        working_memory = "\n\n".join(part for part in working_memory_parts if part)

        user_model_text = ""
        if user_id:
            user_model_text = load_user_model(
                self.config.agent.workspace, user_id
            )

        from .memory.context_loader import load_entity_context
        entity_context_text = load_entity_context(
            self.config.agent.workspace,
            conversation_state=conversation_state,
            task=task,
        )

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
            mode_framing_override=mode_framing_override,
            platform_context=self._load_platform_context(),
            chat_conversation_id=conversation_id if mode == RunMode.CHAT else None,
            preloaded_tool_names=preloaded_tool_names,
        )

    def _build_planned_task(self, task: str, classification: TaskClassification) -> str:
        """For complex tasks, prepend a lightweight execution plan."""
        try:
            planner_model = self._build_model(
                self.config.heartbeat.model or self.config.agent.model
            )
            result = planner_model(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a planning assistant. Given a task, produce a short "
                            "numbered execution plan (3-6 steps). Each step should be a "
                            "concrete action. Be concise — one line per step. "
                            "Output ONLY the numbered list, nothing else."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Task: {task}\n"
                            f"Available skills: {', '.join(classification.relevant_skills)}\n"
                            f"Available servers: {', '.join(classification.relevant_servers)}"
                        ),
                    },
                ],
            )
            plan = result.content if hasattr(result, "content") else str(result)
            logger.info("Generated execution plan:\n%s", plan)
            return f"{task}\n\n## EXECUTION PLAN\n{plan}"
        except Exception as e:
            logger.warning("Planning failed, using raw task: %s", e)
            return task

    def _classify(
        self,
        task: str,
        mode: RunMode,
        conversation_state: Optional[ConversationState] = None,
    ) -> Optional[TaskClassification]:
        """Run the lightweight task classifier (skipped for heartbeats)."""
        if mode == RunMode.HEARTBEAT:
            return None
        try:
            classifier_model = self._build_model(
                self.config.heartbeat.model or self.config.agent.model
            )
            conv_summary = (
                conversation_state.current_topic if conversation_state else None
            )
            classification = classify_task(
                task, classifier_model, conversation_summary=conv_summary
            )
            logger.info(
                "Task classified: intent=%s complexity=%s skills=%s servers=%s",
                classification.intent,
                classification.complexity,
                classification.relevant_skills,
                classification.relevant_servers,
            )
            return classification
        except Exception as e:
            logger.warning("Classification failed, proceeding without: %s", e)
            return None

    @staticmethod
    def _tool_activity_message(tool_name: str) -> str:
        if tool_name == "load_tool":
            return "is preparing a tool"
        if tool_name.startswith("memory_"):
            return "is checking memory"
        if tool_name == "python_interpreter":
            return "is running Python"
        return f"is using {tool_name}"

    def _build_step_callback(
        self,
        status_callback: Optional[RunStatusCallback],
    ) -> Callable[[ActionStep], None]:
        last_message: dict[str, Optional[str]] = {"value": None}
        tracker = self._usage_tracker

        def _emit(message: str) -> None:
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
            logger.info(
                "[Step %d] Tokens so far: in=%s out=%s total=%s",
                getattr(step, "step_number", 0),
                f"{in_tok:,}",
                f"{out_tok:,}",
                f"{in_tok + out_tok:,}",
            )

            if getattr(step, "is_final_answer", False):
                return
            if step.error:
                _emit("hit an error and is retrying")
                return
            tool_calls = getattr(step, "tool_calls", None) or []
            if tool_calls:
                tool_name = getattr(tool_calls[0], "name", None) or "a tool"
                _emit(self._tool_activity_message(tool_name))
                return
            _emit("is thinking about it...")

        return _callback

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
    ) -> str:
        self.connect_mcp()
        model = model_override or self.model

        self._usage_tracker.reset()

        # --- Conversation state (chat mode) ---
        conv_state: Optional[ConversationState] = None
        if mode == RunMode.CHAT and conversation_id:
            conversations_dir = self.config.agent.workspace / "conversations"
            conv_state = load_state(conversations_dir, conversation_id)

            # Pre-turn reflection: if enough turns have passed, curate
            # memories before processing the new message.
            if should_reflect_for_conversation(
                conversations_dir,
                conversation_id,
                conv_state,
                self.config.memory.mid_session_reflection_interval,
            ):
                try:
                    reflection_model = self._build_model(
                        self.config.heartbeat.model or self.config.agent.model
                    )
                    reflection_result = reflect(
                        conv_state, conversations_dir, conversation_id,
                        reflection_model,
                    )
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
                        "Mid-session reflection for %s (turn %d): %d facts, %d prefs",
                        conversation_id,
                        conv_state.turn_count if conv_state else 0,
                        len(reflection_result.facts_to_store),
                        len(reflection_result.user_preferences),
                    )
                except Exception as e:
                    logger.warning("Reflection failed for %s: %s", conversation_id, e)

        classification = self._classify(task, mode, conversation_state=conv_state)

        all_tools, deferred_tool_directory, agent_ref, preloaded_names = (
            self._build_agent_tools(
                mode, user_id=user_id, classification=classification,
                allowed_servers=allowed_servers,
                preload_tools=preload_tools,
            )
        )

        # In chat mode, conversation state replaces the raw history summary
        # in the system prompt.  Raw history is injected as structured steps below.
        system_prompt = self._build_system_prompt(
            task=task,
            mode=mode,
            conversation_id=conversation_id if mode != RunMode.CHAT else None,
            deferred_tool_directory=deferred_tool_directory,
            user_id=user_id,
            classification=classification,
            conversation_state=conv_state,
            mode_framing_override=mode_framing_override,
            preloaded_tool_names=preloaded_names,
        )

        # Memory context is per-query (semantic search), so we place it adjacent
        # to the current task rather than in the system prompt.  This keeps the
        # system prompt stable across turns for better LLM prefix caching.
        # Scheduled tasks use their own learnings system, so skip mem0 for them.
        memory_context = ""
        if not skip_memory:
            retrieval_model = self._build_model(
                self.config.heartbeat.model or self.config.agent.model
            )
            memory_context = retrieve_memories(
                task=task,
                backend=self.memory,
                agent_id=self.config.agent.name,
                config=self.config.memory,
                model=retrieval_model,
                user_id=user_id,
                conversation_state=conv_state,
            )

        effective_task = task
        if memory_context:
            effective_task = (
                f"## Relevant memories\n{memory_context}\n\n"
                f"## Current request\n{task}"
            )
        if classification and classification.needs_planning:
            effective_task = self._build_planned_task(effective_task, classification)

        step_callback = self._build_step_callback(status_callback)
        agent = _SanitizedToolCallingAgent(
            tools=all_tools,
            model=model,
            stream_outputs=bool(response_callback),
            step_callbacks=[step_callback],
        )
        agent_ref["agent"] = agent

        agent.prompt_templates["system_prompt"] = (
            agent.prompt_templates["system_prompt"] + "\n\n" + system_prompt
        )

        # In chat mode, the conversation_summary in ConversationState covers
        # the full history.  We only inject the last 4 turns as structured
        # steps so the model sees recent user/assistant pairs verbatim.
        has_history = False
        if mode == RunMode.CHAT and conversation_id:
            turns = self._load_conversation_turns(conversation_id, limit=4)
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

        # reset=False preserves the injected history steps
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

        # Extract tool call summary from the inner agent before it's discarded
        tool_summary = self._extract_tool_summary(agent)

        # In chat mode, rely on reflection (Phase 5) for curated mem0 storage
        # instead of storing every turn pair.  Scheduled tasks use their own
        # learnings system and skip mem0 to avoid polluting the vector store.
        should_store = (
            mode != RunMode.CHAT
            and not skip_memory
            and (classification is None or classification.worth_remembering)
        )
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

        # Update conversation state after the turn (cheap LLM call)
        if mode == RunMode.CHAT and conversation_id:
            try:
                state_model = self._build_model(
                    self.config.heartbeat.model or self.config.agent.model
                )
                conv_state = update_state(
                    conv_state, task, str(result), state_model
                )
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

        usage = collect_run_usage(agent, model, self._usage_tracker)
        logger.info("Run usage: %s", format_usage_summary(usage))

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

        # Load the autonomous playbook
        heartbeat_path = self.config.agent.workspace / "HEARTBEAT.md"
        if not heartbeat_path.exists():
            return None
        playbook = heartbeat_path.read_text()

        proactive_cfg = self.config.heartbeat.proactive
        servers = proactive_cfg.servers if proactive_cfg.enabled else ["ouro"]

        from .heartbeat import is_within_active_hours
        if not is_within_active_hours(self.config.heartbeat):
            playbook += (
                "\n\n**Note: You are outside active hours. "
                "Only check notifications unless something is urgent.**"
            )

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

            log_entry = f"[heartbeat:{action}] {details}" if details else f"[heartbeat:{action}]"
            write_daily_log(self.config.agent.workspace, log_entry)
        except json.JSONDecodeError:
            pass

        return result

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
