"""Shared MCP tool factories used by both the parent agent and subagents."""

import json
import logging
from typing import Callable, Optional

from smolagents import tool

logger = logging.getLogger(__name__)


def _resolve_tool_name(
    tool_name: str,
    deferred_tools: dict,
    deferred_index: list,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a tool name (qualified or raw) to its qualified name.

    Returns (qualified_name, error_message). Exactly one will be non-None.
    """
    if tool_name in deferred_tools:
        return tool_name, None

    # Build raw_name -> qualified_name mapping for disambiguation
    by_raw: dict[str, list[str]] = {}
    for item in deferred_index:
        by_raw.setdefault(item["raw_name"], []).append(item["tool"])

    candidates = by_raw.get(tool_name, [])
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return (
            None,
            f"Ambiguous tool name '{tool_name}'. Use one of: {', '.join(candidates)}",
        )
    return None, f"Unknown tool '{tool_name}'."


def make_load_tool(
    deferred_tools: dict,
    deferred_index: list,
    agent_ref: dict,
    resolve_fn: Optional[Callable] = None,
):
    """Create a load_tool smolagents @tool backed by a deferred tool directory.

    Args:
        deferred_tools: qualified_name -> tool object mapping
        deferred_index: list of dicts with tool/raw_name/description/inputs/output_type/server
        agent_ref: mutable dict; set agent_ref["agent"] to the running agent instance
            so loaded tools are injected into the live tool set
        resolve_fn: optional custom resolver (tool_name) -> (qualified_name, error).
            Falls back to the built-in _resolve_tool_name if not provided.
    """
    resolver = resolve_fn or (
        lambda name: _resolve_tool_name(name, deferred_tools, deferred_index)
    )

    def _load_one(tool_name: str) -> dict:
        resolved_name, err = resolver(tool_name)
        if err:
            top_examples = [item["tool"] for item in deferred_index[:8]]
            return {
                "error": err,
                "example_tools": top_examples,
                "hint": "Pick from the deferred tool directory in system context.",
            }

        item = next(
            (i for i in deferred_index if i["tool"] == resolved_name), None
        )
        target = deferred_tools.get(resolved_name)
        if not target or not item:
            return {"error": f"Tool '{resolved_name}' not available."}

        raw_name = item["raw_name"]

        running_agent = agent_ref.get("agent")
        if running_agent is not None:
            running_agent.tools[raw_name] = target

        return {
            "status": "loaded",
            "call_as": raw_name,
            "description": item["description"],
            "inputs": item["inputs"],
            "output_type": item["output_type"],
        }

    @tool
    def load_tool(tool_names: list) -> str:
        """Load one or more deferred MCP tools so you can call them directly by name.

        Args:
            tool_names: List of tool name strings from the deferred tool directory.

        Example single:  ["ouro:search_assets"]
        Example multi:   ["ouro:search_assets", "ouro:create_post", "ouro:get_asset"]
        """
        if not tool_names:
            return json.dumps({"error": "No tool names provided."})

        results = [_load_one(name) for name in tool_names]
        if len(results) == 1:
            return json.dumps(results[0])
        return json.dumps(results)

    return load_tool
