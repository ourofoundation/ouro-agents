"""Deterministic cleanup operations triggered by Ouro state changes.

Distinct from ``ouro_agents.refinement``: cleanup is fast, regex-based, and
LLM-free. It handles surgical removals where the right action is unambiguous
(e.g. a deleted asset's UUID is no longer valid anywhere).

The refinement subsystem handles interpretive changes (corrections, guidance
updates) that benefit from an LLM rewriting prose.
"""

from .asset_deleted import (
    SweepResult,
    handle_asset_deleted_webhook,
    sweep_workspace_for_deleted_asset,
)

__all__ = [
    "SweepResult",
    "handle_asset_deleted_webhook",
    "sweep_workspace_for_deleted_asset",
]
