import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from smolagents import LiteLLMModel, ToolCallingAgent, ToolCollection, tool

from .config import MCPServerConfig, OuroAgentsConfig
from .memory import create_memory_backend, format_memories
from .memory.tools import make_memory_tools
from .notes import load_notes
from .skills import load_all_skills
from .soul import build_prompt, load_soul

logger = logging.getLogger(__name__)


class OuroAgent:
    def __init__(self, config: OuroAgentsConfig):
        self.config = config
        self.soul = load_soul(config.agent.workspace / "SOUL.md")
        self.notes = load_notes(config.agent.workspace / "NOTES.md")
        self.skills = load_all_skills(config)
        self.memory = create_memory_backend(config.memory)
        self.memory_tools = make_memory_tools(self.memory, config.agent.name)
        self.model = LiteLLMModel(model_id=config.agent.model)

        self._mcp_contexts: list = []
        self._deferred_tools: dict = {}
        self._deferred_tools_by_raw_name: dict = {}
        self._deferred_index: list[dict] = []
        self._mcp_connected = False

    def connect_mcp(self) -> None:
        """Connect to all configured MCP servers once. Safe to call multiple times."""
        if self._mcp_connected:
            return

        for server in self.config.mcp_servers:
            self._connect_one_server(server)
        self._mcp_connected = True

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
                    server_parameters=server_params, trust_remote_code=True
                )
                collection = ctx.__enter__()
                self._mcp_contexts.append(ctx)
                for mcp_tool in collection.tools:
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

    def _build_agent_tools(self, is_heartbeat: bool = False):
        """Build the tool list and directory string for a single run."""
        deferred_tools = self._deferred_tools
        deferred_index = self._deferred_index

        if is_heartbeat:
            deferred_index = [
                item for item in self._deferred_index if item["server"] == "ouro"
            ]
            ouro_tool_names = {item["tool"] for item in deferred_index}
            deferred_tools = {
                k: v for k, v in self._deferred_tools.items() if k in ouro_tool_names
            }

        agent_self = self

        @tool
        def fetch_tool(tool_name: str) -> str:
            """Fetch the full schema for one deferred MCP tool.
            Args:
                tool_name: Exact tool name, preferably namespaced (e.g. ouro:search_assets)
            """
            resolved_name, err = agent_self._resolve_tool_name(tool_name)
            if err:
                top_examples = [item["tool"] for item in deferred_index[:8]]
                return json.dumps(
                    {
                        "error": err,
                        "example_tools": top_examples,
                        "hint": (
                            "Pick from the deferred tool directory in system context, "
                            "then call fetch_tool with that exact name."
                        ),
                    }
                )

            item = next(i for i in deferred_index if i["tool"] == resolved_name)
            return json.dumps(
                {
                    "tool": item["tool"],
                    "description": item["description"],
                    "inputs": item["inputs"],
                    "output_type": item["output_type"],
                }
            )

        @tool
        def tool_call(tool_name: str, arguments_json: str = "{}") -> str:
            """Call a deferred MCP tool by name with JSON arguments.
            Args:
                tool_name: Tool name from fetch_tool (prefer fully-qualified server:name)
                arguments_json: JSON object string of arguments, e.g. {"org_id":"..."}
            """
            resolved_name, err = agent_self._resolve_tool_name(tool_name)
            if err:
                return json.dumps({"error": f"{err} Call fetch_tool first."})

            try:
                args = json.loads(arguments_json) if arguments_json.strip() else {}
            except Exception as e:
                return json.dumps(
                    {"error": f"arguments_json must be valid JSON object: {e}"}
                )

            if not isinstance(args, dict):
                return json.dumps(
                    {"error": "arguments_json must decode to a JSON object."}
                )

            target = deferred_tools.get(resolved_name)
            if not target:
                return json.dumps({"error": f"Tool '{resolved_name}' not available."})
            try:
                result = target(**args)
            except Exception as e:
                return json.dumps(
                    {"error": f"Tool call failed for '{resolved_name}': {e}"}
                )

            if isinstance(result, str):
                return result
            return json.dumps(result)

        all_tools = list(self.memory_tools) + [fetch_tool, tool_call]

        deferred_tool_directory = "\n".join(
            f"- {item['tool']}: {item['description'][:140]}" for item in deferred_index
        )

        return all_tools, deferred_tool_directory

    async def run(
        self,
        task: str,
        model_override=None,
        conversation_id: Optional[str] = None,
        is_heartbeat: bool = False,
    ) -> str:
        self.connect_mcp()
        model = model_override or self.model

        memories = self.memory.search(task, agent_id=self.config.agent.name)
        memory_context = format_memories(memories)

        skills_text = "" if is_heartbeat else self.skills
        system_prompt = build_prompt(self.soul, self.notes, skills_text, memory_context)

        all_tools, deferred_tool_directory = self._build_agent_tools(is_heartbeat)

        agent = ToolCallingAgent(
            tools=all_tools,
            model=model,
            max_steps=8,
        )

        agent.prompt_templates["system_prompt"] = (
            agent.prompt_templates["system_prompt"]
            + "\n\n"
            + system_prompt
            + "\n\n**MCP tool usage rules**:\n"
            + "- MCP tools are deferred. Pick one from the deferred tool directory below.\n"
            + "- Do NOT guess parameter names. Call `fetch_tool` first, then pass exact args to `tool_call`.\n"
            + "- Prefer fully-qualified names like `ouro:create_post` when calling `tool_call`.\n"
            + "- For content/topic questions on Ouro (e.g. 'what's new in X?'), usually use `ouro:search_assets` (and optionally `ouro:get_team_activity`).\n"
            + "\n\n**Deferred tool directory (name + short description)**:\n"
            + deferred_tool_directory
            + "\n\n**Output format**: For simple replies (greetings, acknowledgments, or when no tools are needed), you must call the `final_answer` tool directly with your response. Never respond with plain text outside a tool call."
        )

        result = agent.run(task)

        # Post-run: store in memory + append to run log
        self.memory.add(
            f"Task: {task}\nResult: {result}",
            agent_id=self.config.agent.name,
            run_id=conversation_id,
        )
        self._log_run(
            task,
            result,
            model.model_id if hasattr(model, "model_id") else str(model),
            is_heartbeat,
        )

        return str(result)

    async def heartbeat(self) -> Optional[str]:
        heartbeat_path = self.config.agent.workspace / "HEARTBEAT.md"
        if not heartbeat_path.exists():
            return None

        checklist = heartbeat_path.read_text()

        # Use heartbeat model if specified, otherwise fallback to primary
        hb_model_id = self.config.heartbeat.model or self.config.agent.model
        hb_model = LiteLLMModel(model_id=hb_model_id)

        result = await self.run(
            checklist,
            model_override=hb_model,
            is_heartbeat=True,
        )

        # Parse structured JSON response
        try:
            # Try to find JSON block in the result
            import re

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

    def _log_run(self, task: str, result: str, model_name: str, is_heartbeat: bool):
        """Append a line to the run log (JSONL)."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "trigger": "heartbeat" if is_heartbeat else "task",
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
