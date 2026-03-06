import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from smolagents import OpenAIModel, ToolCallingAgent, ToolCollection, tool

from .config import MCPServerConfig, OuroAgentsConfig, RunMode
from .memory import create_memory_backend, format_memories
from .memory.tools import make_memory_tools
from .notes import load_notes
from .skills import load_all_skills
from .soul import build_prompt, load_soul
from .tools.python_tool import make_python_tool

logger = logging.getLogger(__name__)


class OuroAgent:
    def __init__(self, config: OuroAgentsConfig):
        self.config = config
        self.soul = load_soul(config.agent.workspace / "SOUL.md")
        self.notes = load_notes(config.agent.workspace / "NOTES.md")
        self.skills = load_all_skills(config)
        self.memory = create_memory_backend(config.memory)
        self.model = self._build_model(config.agent.model)

        self._mcp_contexts: list = []
        self._deferred_tools: dict = {}
        self._deferred_tools_by_raw_name: dict = {}
        self._deferred_index: list[dict] = []
        self._mcp_connected = False

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

    def _build_model(self, model_id: str) -> OpenAIModel:
        model_kwargs = {}
        extra_body = self._build_openrouter_extra_body(model_id)
        if extra_body:
            model_kwargs["extra_body"] = extra_body

        return OpenAIModel(
            model_id=model_id,
            api_base="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            **model_kwargs,
        )

    def _conversation_file(self, conversation_id: str) -> Path:
        conversations_dir = self.config.agent.workspace / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        return conversations_dir / f"{conversation_id}.jsonl"

    def _append_conversation_turn(
        self, conversation_id: str, role: str, content: str
    ) -> None:
        path = self._conversation_file(conversation_id)
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "role": role,
            "content": content,
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

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

    def _format_conversation_turns(self, turns: list[dict]) -> str:
        if not turns:
            return ""

        lines = []
        for turn in turns:
            role = str(turn.get("role", "unknown")).lower()
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            if len(content) > 600:
                content = content[:600] + "..."
            lines.append(f"- {role}: {content}")
        return "\n".join(lines)

    def connect_mcp(self) -> None:
        """Connect to all configured MCP servers once. Safe to call multiple times."""
        if self._mcp_connected:
            return

        for server in self.config.mcp_servers:
            self._connect_one_server(server)
        self._mcp_connected = True

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
        self, mode: RunMode = RunMode.AUTONOMOUS, user_id: Optional[str] = None
    ):
        """Build the tool list and directory string for a single run."""
        deferred_tools = self._deferred_tools
        deferred_index = self._deferred_index

        if mode == RunMode.HEARTBEAT:
            deferred_index = [
                item for item in self._deferred_index if item["server"] == "ouro"
            ]
            ouro_tool_names = {item["tool"] for item in deferred_index}
            deferred_tools = {
                k: v for k, v in self._deferred_tools.items() if k in ouro_tool_names
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
            self.memory, self.config.agent.name, user_id=user_id
        )
        python_tool, _executor = make_python_tool(workspace=self.config.agent.workspace)
        all_tools = list(memory_tools) + [load_tool, python_tool]

        deferred_tool_directory = "\n".join(
            f"- {item['tool']}: {item['description'][:240]}" for item in deferred_index
        )

        return all_tools, deferred_tool_directory, agent_ref

    def _build_system_prompt(
        self,
        task: str,
        mode: RunMode,
        conversation_id: Optional[str],
        deferred_tool_directory: str,
        user_id: Optional[str] = None,
    ) -> str:
        memories = self.memory.search(
            task,
            agent_id=self.config.agent.name,
            user_id=user_id,
        )
        memory_context = format_memories(memories)

        conversation_context = ""
        if conversation_id and mode != RunMode.HEARTBEAT:
            turns = self._load_conversation_turns(conversation_id, limit=12)
            conversation_context = self._format_conversation_turns(turns)

        skills_text = "" if mode == RunMode.HEARTBEAT else self.skills

        return build_prompt(
            soul=self.soul,
            notes=self.notes,
            skills=skills_text,
            memory_context=memory_context,
            mode=mode,
            conversation_context=conversation_context,
            deferred_tool_directory=deferred_tool_directory,
        )

    async def run(
        self,
        task: str,
        model_override=None,
        conversation_id: Optional[str] = None,
        mode: RunMode = RunMode.AUTONOMOUS,
        user_id: Optional[str] = None,
    ) -> str:
        self.connect_mcp()
        model = model_override or self.model

        all_tools, deferred_tool_directory, agent_ref = self._build_agent_tools(
            mode, user_id=user_id
        )

        system_prompt = self._build_system_prompt(
            task=task,
            mode=mode,
            conversation_id=conversation_id,
            deferred_tool_directory=deferred_tool_directory,
            user_id=user_id,
        )

        agent = ToolCallingAgent(
            tools=all_tools,
            model=model,
        )
        agent_ref["agent"] = agent

        agent.prompt_templates["system_prompt"] = (
            agent.prompt_templates["system_prompt"] + "\n\n" + system_prompt
        )

        result = agent.run(task)

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
            self._append_conversation_turn(conversation_id, "assistant", str(result))
        self._log_run(
            task,
            result,
            model.model_id if hasattr(model, "model_id") else str(model),
            mode,
        )

        return str(result)

    async def heartbeat(self) -> Optional[str]:
        heartbeat_path = self.config.agent.workspace / "HEARTBEAT.md"
        if not heartbeat_path.exists():
            return None

        checklist = heartbeat_path.read_text()

        # Use heartbeat model if specified, otherwise fallback to primary
        hb_model_id = self.config.heartbeat.model or self.config.agent.model
        hb_model = self._build_model(hb_model_id)

        result = await self.run(
            checklist,
            model_override=hb_model,
            mode=RunMode.HEARTBEAT,
        )

        # Parse structured JSON response
        try:
            # Try to find JSON block in the result
            json_match = re.search(r"```json\n(.*?)\n```", result, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(1))
            else:
                parsed = json.loads(result)

            if parsed.get("action") == "none":
                return None
        except json.JSONDecodeError:
            # If not valid JSON, assume it's actionable text
            pass

        return result

    def _log_run(self, task: str, result: str, model_name: str, mode: RunMode):
        """Append a line to the run log (JSONL)."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "trigger": mode.value,
            "task_summary": task[:200] + ("..." if len(task) > 200 else ""),
            "model": model_name,
            "result_summary": str(result)[:200]
            + ("..." if len(str(result)) > 200 else ""),
        }
        log_path = self.config.agent.workspace / "runs.jsonl"

        # Ensure workspace exists
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
