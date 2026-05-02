#!/usr/bin/env python
"""Manual one-shot trigger of the refinement runner.

Drains the agent's change-set queue (``workspace/data/change_queue.jsonl``)
through ``ouro_agents.refinement.run_refinement``. Useful for testing the
runner end-to-end even when no producers are wired in to enqueue entries.

Usage::

    cd ouro-agents
    python -m scripts.run_refinement --config config.json --dry-run
    python -m scripts.run_refinement --config config.json --max 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ouro_agents.config import OuroAgentsConfig
from ouro_agents.refinement import ChangeSetQueue, run_refinement


logger = logging.getLogger("run_refinement")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Override the workspace path",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Cap the number of pending changes processed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pending changes and the docs they affect; do not call the LLM",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show DEBUG-level logs"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = OuroAgentsConfig.load_from_file(args.config)
    workspace = Path(args.workspace) if args.workspace else config.agent.workspace
    queue_path = workspace / "data" / "change_queue.jsonl"
    queue = ChangeSetQueue(queue_path)

    pending = queue.pending(limit=args.max)
    if not pending:
        logger.info("No pending change-set entries at %s", queue_path)
        return 0

    logger.info("Pending change-set entries (%d):", len(pending))
    for entry in pending:
        logger.info(
            "  %s [%s] subject=%s team=%s occurred_at=%s",
            entry.id[:8],
            entry.kind.value,
            entry.subject_id,
            entry.team_id or "-",
            entry.occurred_at,
        )

    if args.dry_run:
        from ouro_agents.refinement.runner import collect_affected_docs

        affected = collect_affected_docs(workspace, pending)
        logger.info("Would inspect %d doc(s):", len(affected))
        for path, related in affected.items():
            logger.info(
                "  %s (related: %s)",
                path,
                ", ".join(sorted({c.subject_id for c in related})),
            )
        return 0

    # Real run requires the full agent to build the model + access mem0.
    # Importing OuroAgent here lazily so --dry-run path stays light.
    from ouro_agents.agent import OuroAgent

    agent = OuroAgent(config)
    summary = run_refinement(
        agent=agent,
        queue=queue,
        max_changes_per_pass=args.max
        or config.refinement.max_changes_per_pass,
        max_docs_per_pass=config.refinement.max_docs_per_pass,
        window_lines=config.refinement.window_lines,
    )

    print(
        json.dumps(
            {
                "pending_seen": summary.pending_seen,
                "docs_inspected": summary.docs_inspected,
                "files_rewritten": summary.files_rewritten,
                "windows_applied": summary.windows_applied,
                "memory_deletes": summary.memory_deletes,
                "queue_marked_applied": summary.queue_marked_applied,
                "errors": summary.errors,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
