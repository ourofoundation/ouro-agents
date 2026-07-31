"""Replay a chat conversation to inspect history windowing.

Reads a conversation's transcript from the run log (or a saved fixture), then
replays it turn by turn — reporting which earlier turns would still be injected
under the chosen history policy. Assistant replies come from the recording;
nothing is written back and no platform calls are made.

Usage:
    # Replay a real conversation from the run log
    python scripts/replay_chat.py --config apollo.json --conversation-id <uuid>

    # Save it as a fixture, then replay offline
    python scripts/replay_chat.py --config apollo.json --conversation-id <uuid> \
        --export tests/fixtures/chat/goal_drift_019fb5df.json
    python scripts/replay_chat.py --fixture tests/fixtures/chat/goal_drift_019fb5df.json

    # Compare history policies: today's window vs append-only
    python scripts/replay_chat.py --fixture <path> --window current
    python scripts/replay_chat.py --fixture <path> --window append-only

    # Assert an earlier turn is still visible at a later turn
    python scripts/replay_chat.py --fixture <path> --expect-turn-visible 2:9
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

from ouro_agents.chat_replay import (
    ReplayTurn,
    first_turn_drop,
    format_trajectory,
    load_transcript,
    load_transcript_from_run_log,
    replay,
    save_transcript,
    turn_visible_at,
)
from ouro_agents.config import OuroAgentsConfig
from ouro_agents.run_log import RunLogStore
from ouro_agents.tools.workspace_paths import protected_runs_db
from ouro_agents.utils.conversation import select_history_window


def _identity_window(turns: list[dict]) -> list[dict]:
    return turns


def _load_turns(args) -> list[ReplayTurn]:
    if args.fixture:
        return load_transcript(args.fixture)

    if not args.conversation_id:
        sys.exit("Pass --conversation-id or --fixture")

    config = OuroAgentsConfig.load_from_file(args.config)
    run_log_path = config.run_log.path or protected_runs_db(config.agent.workspace)
    store = RunLogStore(Path(run_log_path), readonly=True)
    if not store.enabled:
        sys.exit(f"No run log at {run_log_path}")
    turns = load_transcript_from_run_log(store, args.conversation_id)
    store.close()
    return turns


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="apollo.json")
    parser.add_argument("--conversation-id", help="Replay from the run log")
    parser.add_argument("--fixture", help="Replay from a saved transcript JSON")
    parser.add_argument("--export", help="Save the transcript to this path and exit")
    parser.add_argument(
        "--window",
        choices=("current", "append-only"),
        default="append-only",
        help="History policy to simulate (default: append-only, the live policy)",
    )
    parser.add_argument(
        "--expect-turn-visible",
        metavar="FROM:AT",
        help="Exit non-zero unless the user turn FROM is still injected at turn AT",
    )
    args = parser.parse_args()

    turns = _load_turns(args)
    if not turns:
        sys.exit("No chat turns found for that conversation")

    if args.export:
        path = Path(args.export)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_transcript(path, turns)
        print(f"Wrote {len(turns)} turns to {path}")
        return

    window_fn = (
        select_history_window if args.window == "current" else _identity_window
    )

    steps = replay(turns, window_fn=window_fn)
    print(f"=== {len(turns)} turns, window={args.window} ===")
    print(format_trajectory(steps))

    if args.expect_turn_visible:
        try:
            source_s, at_s = args.expect_turn_visible.split(":", 1)
            source_index = int(source_s)
            at_index = int(at_s)
        except ValueError:
            sys.exit("--expect-turn-visible must be FROM:AT (e.g. 2:9)")
        if turn_visible_at(steps, source_index=source_index, at_index=at_index):
            print(f"\nOK: turn {source_index} is still visible at turn {at_index}")
        else:
            lost = first_turn_drop(steps, source_index)
            where = (
                f" (dropped at turn {lost.turn.index}: {lost.turn.user[:80]!r})"
                if lost
                else ""
            )
            sys.exit(
                f"\nFAIL: turn {source_index} is not visible at turn {at_index}{where}"
            )


if __name__ == "__main__":
    main()
