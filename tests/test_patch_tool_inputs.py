"""MCP tool input schema patching for OpenAI compatibility."""

from types import SimpleNamespace

from ouro_agents.agent import OuroAgent


# Zod z.email() pattern (resend-mcp send_email `to` items).
_ZOD_EMAIL_PATTERN = (
    r"^(?!\.)(?!.*\.\.)([A-Za-z0-9_'+\-\.]*)[A-Za-z0-9_+-]@"
    r"([A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}$"
)


def test_strips_lookaround_pattern_from_nested_items():
    tool = SimpleNamespace(
        inputs={
            "to": {
                "type": "array",
                "description": "Recipients",
                "items": {
                    "type": "string",
                    "format": "email",
                    "pattern": _ZOD_EMAIL_PATTERN,
                },
            },
            "subject": {
                "type": "string",
                "description": "Subject",
                "pattern": r"^[\w ]+$",
            },
        }
    )

    OuroAgent._patch_tool_inputs(tool)

    assert "pattern" not in tool.inputs["to"]["items"]
    assert tool.inputs["to"]["items"]["format"] == "email"
    # Non-lookaround patterns are left alone.
    assert tool.inputs["subject"]["pattern"] == r"^[\w ]+$"


def test_collapses_nullable_anyof():
    tool = SimpleNamespace(
        inputs={
            "cc": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            }
        }
    )

    OuroAgent._patch_tool_inputs(tool)

    assert tool.inputs["cc"]["type"] == "string"
    assert tool.inputs["cc"]["nullable"] is True
    assert "anyOf" not in tool.inputs["cc"]
