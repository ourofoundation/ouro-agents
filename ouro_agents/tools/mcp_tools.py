"""Shared MCP tool factories used by both the parent agent and subagents."""

import json
import logging
import re
from typing import Callable, Optional

from smolagents import tool

logger = logging.getLogger(__name__)


# Strips a leading bold label like "**Purpose:**" that some MCP servers prepend
# to every tool description, so the directory leads with actual meaning.
_TOOL_DESC_LABEL_RE = re.compile(r"^\*\*[^*]+:\*\*\s*")


def short_tool_description(description: str, max_chars: int = 110) -> str:
    """One clean line per tool for the directory: no label boilerplate, no
    mid-word truncation. Prefer the first sentence; otherwise cut on a word."""
    text = _TOOL_DESC_LABEL_RE.sub("", description or "").strip()
    period = text.find(". ")
    if 0 < period + 1 <= max_chars:
        return text[: period + 1]
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    return text[:cut].rstrip() + "…"


def format_deferred_directory(
    deferred_index: list,
    primary_servers: set,
    server_descriptions: Optional[dict] = None,
) -> str:
    """Render the deferred tool directory grouped by server.

    Primary servers (the ones an agent reaches for constantly) get a clean
    one-line description per tool. Secondary servers collapse to a single line —
    name, tool count, and a short summary — so a directory of 100+ peripheral
    tools costs a couple of lines instead of dominating every turn. The agent
    expands a collapsed server on demand with ``load_tool(["<server>"])``.
    """
    server_descriptions = server_descriptions or {}
    by_server: dict[str, list[dict]] = {}
    for item in deferred_index:
        by_server.setdefault(item["server"], []).append(item)

    ordered = sorted(by_server, key=lambda s: (s not in primary_servers, s))
    blocks: list[str] = []
    for server in ordered:
        items = by_server[server]
        if server in primary_servers:
            blocks.append(
                "\n".join(
                    f"- {it['tool']}: {short_tool_description(it['description'])}"
                    for it in items
                )
            )
        else:
            noun = "tool" if len(items) == 1 else "tools"
            desc = (server_descriptions.get(server) or "").strip()
            desc_part = f" {desc}" if desc else ""
            blocks.append(
                f"`{server}` — {len(items)} {noun}.{desc_part} "
                f'Call `load_tool(["{server}"])` to list its {noun}.'
            )
    return "\n\n".join(blocks)


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
    server_descriptions: Optional[dict] = None,
):
    """Create a load_tool smolagents @tool backed by a deferred tool directory.

    Args:
        deferred_tools: qualified_name -> tool object mapping
        deferred_index: list of dicts with tool/raw_name/description/inputs/output_type/server
        agent_ref: mutable dict; set agent_ref["agent"] to the running agent instance
            so loaded tools are injected into the live tool set
        resolve_fn: optional custom resolver (tool_name) -> (qualified_name, error).
            Falls back to the built-in _resolve_tool_name if not provided.
        server_descriptions: optional server_name -> one-line summary, surfaced
            when a collapsed server is expanded.
    """
    resolver = resolve_fn or (
        lambda name: _resolve_tool_name(name, deferred_tools, deferred_index)
    )
    server_descriptions = server_descriptions or {}
    servers = {item["server"] for item in deferred_index}

    def _expand_server(server: str) -> dict:
        items = [it for it in deferred_index if it["server"] == server]
        result = {
            "server": server,
            "tool_count": len(items),
            "tools": [
                {
                    "name": it["tool"],
                    "description": short_tool_description(it["description"]),
                }
                for it in items
            ],
            "hint": "These tools are not loaded yet. Call load_tool again with one "
            "or more of the names above to load them, then call them directly.",
        }
        desc = (server_descriptions.get(server) or "").strip()
        if desc:
            result["description"] = desc
        return result

    def _load_one(tool_name: str) -> dict:
        # A bare server name (or "server:") lists that server's tools without
        # loading anything — the way an agent browses a collapsed server.
        server_key = tool_name[:-1] if tool_name.endswith(":") else tool_name
        if ":" not in server_key and server_key in servers:
            return _expand_server(server_key)

        resolved_name, err = resolver(tool_name)
        if err:
            top_examples = [item["tool"] for item in deferred_index[:8]]
            return {
                "error": err,
                "example_tools": top_examples,
                "hint": "Pick from the deferred tool directory in system context.",
            }

        item = next((i for i in deferred_index if i["tool"] == resolved_name), None)
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
            # "output_type": item["output_type"], # probably not needed
        }

    @tool
    def load_tool(tool_names: list[str]) -> str:
        """Load deferred MCP tools, or browse a collapsed server's tools.

        Two uses, mixable in one call:
        - Pass tool names to load them so you can call them directly afterward.
        - Pass a bare server name to list that server's tools without loading
          anything. Collapsed servers in the directory show only a name and
          summary; expand one this way, then load the specific tools you need.

        Args:
            tool_names: List of tool names and/or server names.

        Example load:    ["ouro:search_assets", "ouro:create_post"]
        Example browse:  ["resend"]
        Example mixed:   ["resend:send_email", "search"]
        """
        if not tool_names:
            return json.dumps({"error": "No tool names provided."})

        results = [_load_one(name) for name in tool_names]
        if len(results) == 1:
            return json.dumps(results[0])
        return json.dumps(results)

    return load_tool
