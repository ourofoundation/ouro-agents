"""Tests for per-tool attribution of parallel observation blobs."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from smolagents.memory import ActionStep, ToolCall
from smolagents.monitoring import Timing

from ouro_agents.utils.conversation import extract_tool_summary
from ouro_agents.utils.message_persistence import extract_tool_call_payloads
from ouro_agents.utils.tool_observations import (
    attribute_observation_results,
    split_labeled_observations,
)


def _labeled_blob() -> str:
    return (
        "=== Tool result: run_shell (id=call_find) ===\n"
        "[exit_code]\n0\n[stdout]\n/workspace/coils/send-and-log\n"
        "=== Tool result: run_shell (id=call_ls) ===\n"
        "[exit_code]\n0\n[stdout]\noutreach.md\n"
        "=== Tool result: load_skill (id=call_skill) ===\n"
        "## Skill: coils\n# Coils\n\nTurn repeated multi-step work into a coil.\n"
        "extends: <parent>\n"
    )


class TestSplitLabeledObservations(unittest.TestCase):
    def test_splits_by_call_id(self):
        by_id = split_labeled_observations(_labeled_blob())
        self.assertIsNotNone(by_id)
        assert by_id is not None  # narrow for type checkers
        self.assertEqual(
            by_id["call_find"],
            "[exit_code]\n0\n[stdout]\n/workspace/coils/send-and-log",
        )
        self.assertEqual(by_id["call_ls"], "[exit_code]\n0\n[stdout]\noutreach.md")
        self.assertIn("## Skill: coils", by_id["call_skill"])
        self.assertIn("extends: <parent>", by_id["call_skill"])

    def test_unlabeled_returns_none(self):
        self.assertIsNone(split_labeled_observations("plain stdout"))


class TestAttributeObservationResults(unittest.TestCase):
    def test_parallel_calls_get_distinct_bodies(self):
        calls = [
            {"id": "call_find", "name": "run_shell", "arguments": {"command": "find"}},
            {"id": "call_ls", "name": "run_shell", "arguments": {"command": "ls"}},
            {
                "id": "call_skill",
                "name": "load_skill",
                "arguments": {"skill_names": ["coils"]},
            },
        ]
        results = attribute_observation_results(calls, _labeled_blob())
        self.assertEqual(len(results), 3)
        self.assertIn("/workspace/coils/send-and-log", results[0])
        self.assertIn("outreach.md", results[1])
        self.assertIn("## Skill: coils", results[2])
        self.assertNotEqual(results[0], results[1])
        self.assertNotEqual(results[1], results[2])
        # load_skill must not start with the first shell result
        self.assertFalse(results[2].startswith("[exit_code]"))

    def test_single_unlabeled_keeps_full_obs(self):
        calls = [{"id": "call_1", "name": "run_shell", "arguments": {}}]
        results = attribute_observation_results(calls, "hello world")
        self.assertEqual(results, ["hello world"])

    def test_unlabeled_multi_uses_placeholder(self):
        calls = [
            {"id": "call_1", "name": "a", "arguments": {}},
            {"id": "call_2", "name": "b", "arguments": {}},
        ]
        results = attribute_observation_results(calls, "combined")
        self.assertEqual(results[0], "combined")
        self.assertIn("first tool call", results[1])


class TestExtractToolCallPayloads(unittest.TestCase):
    def test_parallel_step_payloads_are_distinct(self):
        step = ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.0),
            tool_calls=[
                ToolCall(name="run_shell", arguments={"command": "find"}, id="call_find"),
                ToolCall(name="run_shell", arguments={"command": "ls"}, id="call_ls"),
                ToolCall(
                    name="load_skill",
                    arguments={"skill_names": ["coils"]},
                    id="call_skill",
                ),
            ],
            observations=_labeled_blob(),
        )
        payloads = extract_tool_call_payloads(step)
        self.assertEqual([p["name"] for p in payloads], [
            "run_shell",
            "run_shell",
            "load_skill",
        ])
        self.assertIn("/workspace/coils/send-and-log", payloads[0]["result"])
        self.assertIn("outreach.md", payloads[1]["result"])
        self.assertIn("## Skill: coils", payloads[2]["result"])
        self.assertNotIn("/workspace/coils/send-and-log", payloads[2]["result"])
        self.assertEqual(payloads[2]["id"], "call_skill")


class TestExtractToolSummary(unittest.TestCase):
    def test_summary_attributes_per_call(self):
        step = ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.0),
            tool_calls=[
                ToolCall(name="run_shell", arguments={"command": "find"}, id="call_find"),
                ToolCall(
                    name="load_skill",
                    arguments={"skill_names": ["coils"]},
                    id="call_skill",
                ),
            ],
            observations=(
                "=== Tool result: run_shell (id=call_find) ===\n"
                "find-out\n"
                "=== Tool result: load_skill (id=call_skill) ===\n"
                "## Skill: coils\nfull skill body here"
            ),
        )
        agent = SimpleNamespace(memory=SimpleNamespace(steps=[step]))
        summary = extract_tool_summary(agent, for_persistence=True)
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["result"], "find-out")
        self.assertIn("## Skill: coils", summary[1]["result"])
        self.assertNotEqual(summary[0]["result"], summary[1]["result"])


if __name__ == "__main__":
    unittest.main()
