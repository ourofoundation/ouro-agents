"""Tests for loading chat turns from platform messages."""

from __future__ import annotations

from ouro_agents.utils.conversation import (
    INTERRUPTED_REPLY_PREFIX,
    build_history_steps,
    messages_to_turns,
)


AGENT_ID = "agent-user-id"
HUMAN_ID = "human-user-id"


def test_messages_to_turns_pairs_user_and_agent_final():
    user_turn_id = "turn-user-1"
    agent_turn_id = "turn-agent-1"
    messages = [
        {
            "id": "m1",
            "turn_id": user_turn_id,
            "seq": 0,
            "type": "message",
            "text": "Hello there",
            "user_id": HUMAN_ID,
            "created_at": "2026-06-11T10:00:00Z",
            "metadata": {},
        },
        {
            "id": "m2",
            "turn_id": agent_turn_id,
            "seq": 0,
            "type": "reasoning",
            "text": "Thinking...",
            "user_id": AGENT_ID,
            "created_at": "2026-06-11T10:00:01Z",
            "metadata": {},
        },
        {
            "id": "m3",
            "turn_id": agent_turn_id,
            "seq": 1,
            "type": "tool_call",
            "text": "Called search",
            "user_id": AGENT_ID,
            "created_at": "2026-06-11T10:00:02Z",
            "json": {
                "name": "search_assets",
                "arguments": {"query": "widgets"},
                "result": "3 results",
            },
            "metadata": {},
        },
        {
            "id": "m4",
            "turn_id": agent_turn_id,
            "seq": 2,
            "type": "message",
            "text": "Here are three widgets.",
            "user_id": AGENT_ID,
            "created_at": "2026-06-11T10:00:03Z",
            "metadata": {"turn_final": True},
        },
    ]

    turns = messages_to_turns(messages, agent_user_id=AGENT_ID)

    assert turns == [
        {
            "role": "user",
            "content": "Hello there",
            "timestamp": "2026-06-11T10:00:00Z",
        },
        {
            "role": "assistant",
            "content": "Here are three widgets.",
            "timestamp": "2026-06-11T10:00:03Z",
            "tool_summary": [
                {
                    "tool": "search_assets",
                    "args": {"query": "widgets"},
                    "result": "3 results",
                }
            ],
        },
    ]


def test_messages_to_turns_prefers_interrupted_reply_and_excludes_trigger_turn():
    user_turn_id = "turn-user-current"
    agent_turn_id = "turn-agent-interrupted"
    messages = [
        {
            "id": "old-user",
            "turn_id": "turn-user-old",
            "seq": 0,
            "type": "message",
            "text": "Earlier question",
            "user_id": HUMAN_ID,
            "created_at": "2026-06-11T09:00:00Z",
            "metadata": {},
        },
        {
            "id": "old-agent",
            "turn_id": "turn-agent-old",
            "seq": 1,
            "type": "message",
            "text": "Earlier answer",
            "user_id": AGENT_ID,
            "created_at": "2026-06-11T09:00:01Z",
            "metadata": {"turn_final": True},
        },
        {
            "id": "current-user",
            "turn_id": user_turn_id,
            "seq": 0,
            "type": "message",
            "text": "New question",
            "user_id": HUMAN_ID,
            "created_at": "2026-06-11T10:00:00Z",
            "metadata": {},
        },
        {
            "id": "interrupted",
            "turn_id": agent_turn_id,
            "seq": 1,
            "type": "message",
            "text": (
                f"{INTERRUPTED_REPLY_PREFIX}\n\n"
                "Your reply up to the cut-off:\nPartial answer"
            ),
            "user_id": AGENT_ID,
            "created_at": "2026-06-11T10:00:02Z",
            "metadata": {"turn_final": True, "interrupted": True},
        },
    ]

    turns = messages_to_turns(
        messages,
        agent_user_id=AGENT_ID,
        exclude_turn_ids={user_turn_id},
    )

    assert turns == [
        {
            "role": "user",
            "content": "Earlier question",
            "timestamp": "2026-06-11T09:00:00Z",
        },
        {
            "role": "assistant",
            "content": "Earlier answer",
            "timestamp": "2026-06-11T09:00:01Z",
            "tool_summary": None,
        },
        {
            "role": "assistant",
            "content": (
                f"{INTERRUPTED_REPLY_PREFIX}\n\n"
                "Your reply up to the cut-off:\nPartial answer"
            ),
            "timestamp": "2026-06-11T10:00:02Z",
            "tool_summary": None,
        },
    ]

    steps = build_history_steps(turns)
    assert len(steps) == 3
