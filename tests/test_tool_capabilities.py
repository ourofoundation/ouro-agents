import re
from pathlib import Path

from ouro_agents.security.policy import Capability
from ouro_agents.security.tool_capabilities import (
    capability_for_tool,
    filter_deferred_tools,
    unmapped_tools,
)


def _ouro_mcp_tool_names() -> list[str]:
    repo_root = Path(__file__).resolve().parents[2]
    tools_dir = repo_root / "ouro-mcp" / "src" / "ouro_mcp" / "tools"
    names: list[str] = []
    for path in sorted(tools_dir.glob("*.py")):
        pending_tool = False
        for line in path.read_text().splitlines():
            if "@mcp.tool" in line:
                pending_tool = True
                continue
            if pending_tool:
                match = re.match(r"\s+def\s+([a-zA-Z_][a-zA-Z0-9_]*)\(", line)
                if match:
                    names.append(f"ouro:{match.group(1)}")
                    pending_tool = False
    return names


def test_all_in_repo_ouro_mcp_tools_have_capabilities():
    tool_names = _ouro_mcp_tool_names()

    assert tool_names
    assert unmapped_tools(tool_names) == []


def test_known_external_servers_are_classified():
    assert capability_for_tool("search:web_search_exa") is Capability.EXTERNAL_SEARCH
    assert capability_for_tool("resend:send_email") is Capability.SEND_MESSAGE
    assert capability_for_tool("unknown:danger") is None


def test_filter_deferred_tools_is_default_deny():
    deferred_tools = {
        "ouro:get_asset": object(),
        "ouro:execute_route": object(),
        "unknown:danger": object(),
    }
    deferred_index = [
        {"tool": name, "server": name.split(":", 1)[0], "raw_name": name}
        for name in deferred_tools
    ]

    filtered_tools, filtered_index = filter_deferred_tools(
        deferred_tools,
        deferred_index,
        {Capability.READ_PLATFORM},
    )

    assert set(filtered_tools) == {"ouro:get_asset"}
    assert [item["tool"] for item in filtered_index] == ["ouro:get_asset"]
