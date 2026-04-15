import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _load_reflection_modules():
    repo_root = Path(__file__).resolve().parents[1]
    package_dir = repo_root / "ouro_agents"

    if "ouro_agents" not in sys.modules:
        package = types.ModuleType("ouro_agents")
        package.__path__ = [str(package_dir)]
        sys.modules["ouro_agents"] = package

    if "ouro_agents.subagents" not in sys.modules:
        subagents_package = types.ModuleType("ouro_agents.subagents")
        subagents_package.__path__ = [str(package_dir / "subagents")]
        sys.modules["ouro_agents.subagents"] = subagents_package

    if "ouro_agents.memory" not in sys.modules:
        memory_package = types.ModuleType("ouro_agents.memory")
        memory_package.__path__ = [str(package_dir / "memory")]
        sys.modules["ouro_agents.memory"] = memory_package

    reflector_spec = importlib.util.spec_from_file_location(
        "ouro_agents.subagents.reflector",
        package_dir / "subagents" / "reflector.py",
    )
    reflector_module = importlib.util.module_from_spec(reflector_spec)
    sys.modules["ouro_agents.subagents.reflector"] = reflector_module
    assert reflector_spec and reflector_spec.loader
    reflector_spec.loader.exec_module(reflector_module)

    reflection_spec = importlib.util.spec_from_file_location(
        "ouro_agents.memory.reflection",
        package_dir / "memory" / "reflection.py",
    )
    reflection_module = importlib.util.module_from_spec(reflection_spec)
    sys.modules["ouro_agents.memory.reflection"] = reflection_module
    assert reflection_spec and reflection_spec.loader
    reflection_spec.loader.exec_module(reflection_module)

    return reflector_module, reflection_module


_reflector_module, _reflection_module = _load_reflection_modules()
ReflectionResult = _reflector_module.ReflectionResult
parse_reflection_result = _reflector_module.parse_reflection_result
build_run_reflection_task = _reflector_module.build_run_reflection_task
REFLECTOR_PROMPT = _reflector_module.REFLECTOR_PROMPT
apply_reflection = _reflection_module.apply_reflection


class _FakeMemoryBackend:
    def __init__(self):
        self.items = []

    def add(self, text, agent_id=None, user_id=None, run_id=None, metadata=None, team_id=None):
        self.items.append(
            {
                "text": text,
                "agent_id": agent_id,
                "user_id": user_id,
                "run_id": run_id,
                "metadata": metadata or {},
                "team_id": team_id,
            }
        )


class _ConversationState:
    def __init__(self, turn_count):
        self.turn_count = turn_count


class TestReflectionParsing(unittest.TestCase):
    def test_reflector_prompt_mentions_recent_asset_interactions(self):
        self.assertIn("avoid repeating immediately", REFLECTOR_PROMPT)
        self.assertIn("already touched this recently", REFLECTOR_PROMPT)

    def test_returns_none_when_reflector_hits_max_steps(self):
        self.assertIsNone(parse_reflection_result("Reached max steps."))

    def test_parses_valid_empty_reflection_payload(self):
        result = parse_reflection_result(
            '{"facts_to_store": [], "user_preferences": [], "daily_log_entry": ""}'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.facts_to_store, [])
        self.assertEqual(result.user_preferences, [])
        self.assertEqual(result.daily_log_entry, "")

    def test_build_run_reflection_task_mentions_redundant_follow_up_avoidance(self):
        task = build_run_reflection_task(
            task="Comment on a post if useful.",
            result="Left a comment on asset abc.",
            tool_summary=[{"tool": "ouro:create_comment", "result": "commented on asset abc"}],
            run_mode="heartbeat",
        )

        self.assertIn("already touched recently", task)
        self.assertIn("avoid redundant follow-up", task)

    def test_build_run_reflection_task_filters_noisy_tools_keeps_order(self):
        task = build_run_reflection_task(
            task="Review feed and respond where helpful.",
            result="Commented twice and updated a post.",
            tool_summary=[
                {"tool": "memory_recall", "result": "prior context"},
                {"tool": "ouro:get_asset", "result": "asset body"},
                {"tool": "ouro:create_comment", "result": "comment one"},
                {"tool": "load_tool", "result": "loaded search"},
                {"tool": "ouro:update_post", "result": "updated plan post"},
                {"tool": "ouro:create_comment", "result": "comment two"},
            ],
            run_mode="heartbeat",
        )

        self.assertNotIn("memory_recall", task)
        self.assertNotIn("ouro:get_asset", task)
        self.assertNotIn("load_tool", task)
        self.assertIn("- ouro:create_comment: comment one", task)
        self.assertIn("- ouro:update_post: updated plan post", task)
        self.assertIn("- ouro:create_comment: comment two", task)
        self.assertLess(
            task.index("- ouro:create_comment: comment one"),
            task.index("- ouro:update_post: updated plan post"),
        )
        self.assertLess(
            task.index("- ouro:update_post: updated plan post"),
            task.index("- ouro:create_comment: comment two"),
        )

    def test_build_run_reflection_task_includes_all_non_noisy_tools(self):
        tool_summary = [
            {"tool": "ouro:create_comment", "result": f"comment {i}"} for i in range(12)
        ]
        task = build_run_reflection_task(
            task="Comment when appropriate.",
            result="Left many comments.",
            tool_summary=tool_summary,
            run_mode="heartbeat",
        )

        self.assertIn("- ouro:create_comment: comment 0", task)
        self.assertIn("- ouro:create_comment: comment 11", task)


class TestApplyReflection(unittest.TestCase):
    def test_valid_reflection_stores_fact_and_marks_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            conversations_dir = workspace / "conversations"
            backend = _FakeMemoryBackend()
            result = ReflectionResult(
                facts_to_store=[
                    {
                        "text": "User prefers concise updates.",
                        "category": "observation",
                        "importance": 0.7,
                    }
                ],
                user_preferences=[],
                daily_log_entry="",
            )

            apply_reflection(
                result,
                backend,
                agent_id="hermes",
                user_id="user-1",
                conversation_id="conv-1",
                workspace=workspace,
                conversations_dir=conversations_dir,
                conversation_state=_ConversationState(turn_count=12),
            )

            self.assertEqual(len(backend.items), 1)
            self.assertEqual(backend.items[0]["text"], "User prefers concise updates.")
            self.assertEqual(
                (conversations_dir / "conv-1.reflected").read_text(),
                "12",
            )

    def test_valid_reflection_preserves_team_id_on_memory_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            conversations_dir = workspace / "conversations"
            backend = _FakeMemoryBackend()
            result = ReflectionResult(
                facts_to_store=[
                    {
                        "text": "Team-specific preference.",
                        "category": "observation",
                        "importance": 0.8,
                    }
                ],
                user_preferences=[],
                daily_log_entry="",
            )

            apply_reflection(
                result,
                backend,
                agent_id="hermes",
                user_id="user-1",
                conversation_id="conv-1",
                workspace=workspace,
                conversations_dir=conversations_dir,
                conversation_state=_ConversationState(turn_count=5),
                team_id="team-42",
            )

            self.assertEqual(backend.items[0]["team_id"], "team-42")


if __name__ == "__main__":
    unittest.main()
