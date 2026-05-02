"""LLM-driven refinement of agent learnings.

Distinct from ``ouro_agents.cleanup``: refinement is interpretive. A
typed ``ChangeSetQueue`` accumulates events that the agent should react to
(corrections, guidance updates, etc.); the runner drains the queue, scopes
work to docs that mention the affected subjects, and uses a cheap LLM to
edit only what's relevant.

Asset deletions are explicitly NOT a queue producer — see
``ouro_agents.cleanup.asset_deleted`` for that path.
"""

from .queue import ChangeEntry, ChangeKind, ChangeSetQueue
from .runner import RefinementSummary, run_refinement

__all__ = [
    "ChangeEntry",
    "ChangeKind",
    "ChangeSetQueue",
    "RefinementSummary",
    "run_refinement",
]
