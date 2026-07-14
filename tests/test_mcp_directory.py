import json
from pathlib import Path

from smolagents.models import get_tool_json_schema

from ouro_agents.memory.tools import make_memory_tools
from ouro_agents.tools.mcp_tools import (
    format_deferred_directory,
    make_load_tool,
    short_tool_description,
)
from ouro_agents.tools.skills_tools import make_load_skill_tool


def _index():
    return [
        {
            "tool": "ouro:search_assets",
            "server": "ouro",
            "raw_name": "search_assets",
            "description": "Search or browse assets on Ouro. Supports hybrid search.",
            "inputs": {"query": {"type": "string"}},
            "output_type": "string",
        },
        {
            "tool": "resend:send_email",
            "server": "resend",
            "raw_name": "send_email",
            "description": "**Purpose:** Send a single transactional email immediately or scheduled.",
            "inputs": {},
            "output_type": "string",
        },
        {
            "tool": "resend:list_broadcasts",
            "server": "resend",
            "raw_name": "list_broadcasts",
            "description": "**Purpose:** List all broadcast campaigns (newsletters/bulk emails).",
            "inputs": {},
            "output_type": "string",
        },
    ]


def test_short_description_strips_label_and_keeps_first_sentence():
    assert short_tool_description("**Purpose:** Send an email. Then more text.") == (
        "Send an email."
    )


def test_short_description_cuts_on_word_boundary_without_label():
    text = "Create " + "alpha " * 50  # long, no sentence break
    out = short_tool_description(text, max_chars=40)
    assert out.endswith("…")
    assert " alph…" not in out  # no mid-word cut
    assert len(out) <= 41


def test_directory_expands_primary_collapses_secondary():
    out = format_deferred_directory(
        _index(),
        primary_servers={"ouro"},
        server_descriptions={"resend": "Email platform."},
    )
    # Primary server: one line per tool.
    assert "- ouro:search_assets: Search or browse assets on Ouro." in out
    # Secondary server: collapsed, no individual tool names.
    assert "`resend` — 2 tools. Email platform." in out
    assert 'load_tool(["resend"])' in out
    assert "resend:send_email" not in out


def test_load_tool_expands_a_server_without_loading():
    agent_ref = {}
    load_tool = make_load_tool(
        {"resend:send_email": object(), "resend:list_broadcasts": object()},
        _index(),
        agent_ref,
        server_descriptions={"resend": "Email platform."},
    )
    result = json.loads(load_tool(["resend"]))
    assert result["server"] == "resend"
    assert result["tool_count"] == 2
    names = {t["name"] for t in result["tools"]}
    assert names == {"resend:send_email", "resend:list_broadcasts"}
    assert result["description"] == "Email platform."
    # Browsing must not inject tools into the live agent.
    assert agent_ref == {}


def test_load_tool_still_loads_a_qualified_tool():
    agent_ref = {}

    class FakeAgent:
        def __init__(self):
            self.tools = {}

    agent = FakeAgent()
    agent_ref["agent"] = agent
    target = object()
    load_tool = make_load_tool(
        {"resend:send_email": target},
        [_index()[1]],
        agent_ref,
    )
    result = json.loads(load_tool(["resend:send_email"]))
    assert result["status"] == "loaded"
    assert result["call_as"] == "send_email"
    assert agent.tools["send_email"] is target


def _array_items(tool, param: str) -> dict:
    props = get_tool_json_schema(tool)["function"]["parameters"]["properties"]
    items = props[param].get("items")
    assert items is not None, f"{tool.name}.{param} missing items in schema"
    return items


def test_batch_tool_schemas_include_array_items(tmp_path: Path):
    """Bare `list` annotations omit `items`, which makes some models emit []."""

    class _Backend:
        def search(self, *args, **kwargs):
            return []

        def count(self):
            return 0

    load_tool = make_load_tool({}, [], {})
    assert _array_items(load_tool, "tool_names") == {"type": "string"}

    load_skill = make_load_skill_tool(tmp_path)
    assert _array_items(load_skill, "skill_names") == {"type": "string"}

    memory_recall = next(
        t for t in make_memory_tools(_Backend(), agent_id="hermes") if t.name == "memory_recall"
    )
    assert _array_items(memory_recall, "queries")["type"] == "object"
