"""Shared constants used across the ouro-agents package."""

CHARS_PER_TOKEN = 4
"""Rough estimate of characters per token for budget calculations."""

GLOBAL_ORG_UUID = "00000000-0000-0000-0000-000000000000"
"""Ouro global (personal) organization id when no specific org is set."""

FETCHABLE_ASSET_TYPES = frozenset(
    {"post", "comment", "file", "dataset", "service", "route"}
)
"""Asset types that can be retrieved via ouro.assets.retrieve / get_asset."""
