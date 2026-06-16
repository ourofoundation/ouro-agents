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
        memory_spec = importlib.util.spec_from_file_location(
            "ouro_agents.memory",
            package_dir / "memory" / "__init__.py",
            submodule_search_locations=[str(package_dir / "memory")],
        )
        memory_package = importlib.util.module_from_spec(memory_spec)
        sys.modules["ouro_agents.memory"] = memory_package
        assert memory_spec and memory_spec.loader
        memory_spec.loader.exec_module(memory_package)

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
DailyLogEntry = _reflector_module.DailyLogEntry
ReflectionResult = _reflector_module.ReflectionResult
parse_reflection_result = _reflector_module.parse_reflection_result
build_run_reflection_task = _reflector_module.build_run_reflection_task
REFLECTOR_PROMPT = _reflector_module.REFLECTOR_PROMPT
apply_reflection = _reflection_module.apply_reflection
record_reflection_turn = _reflection_module.record_reflection_turn
should_reflect = _reflection_module.should_reflect
should_reflect_for_conversation = _reflection_module.should_reflect_for_conversation
validated_daily_log_entries = _reflection_module.validated_daily_log_entries


class _FakeMemoryBackend:
    def __init__(self):
        self.items = []

    def add(
        self,
        text,
        agent_id=None,
        user_id=None,
        run_id=None,
        metadata=None,
        team_id=None,
        infer=True,
    ):
        self.items.append(
            {
                "text": text,
                "agent_id": agent_id,
                "user_id": user_id,
                "run_id": run_id,
                "metadata": metadata or {},
                "team_id": team_id,
                "infer": infer,
            }
        )


class _ConversationState:
    def __init__(self, turn_count):
        self.turn_count = turn_count


class TestConversationTurnCount(unittest.TestCase):
    def test_update_state_increments_once_per_exchange(self):
        conversation_state_spec = importlib.util.spec_from_file_location(
            "ouro_agents.memory.conversation_state",
            Path(__file__).resolve().parents[1]
            / "ouro_agents"
            / "memory"
            / "conversation_state.py",
        )
        conversation_state_module = importlib.util.module_from_spec(
            conversation_state_spec
        )
        assert conversation_state_spec and conversation_state_spec.loader
        conversation_state_spec.loader.exec_module(conversation_state_module)

        ConversationState = conversation_state_module.ConversationState
        update_state = conversation_state_module.update_state

        class _Model:
            def __call__(self, _messages):
                return types.SimpleNamespace(
                    content=(
                        '{"current_topic": "topic", "active_goals": [], '
                        '"decisions_made": [], "open_questions": [], '
                        '"key_entities": [], "key_moments": [], '
                        '"conversation_summary": "summary", "turn_count": 99}'
                    )
                )

        previous = ConversationState(turn_count=4)
        updated = update_state(previous, "hello", "hi there", _Model())
        self.assertEqual(updated.turn_count, 5)


class TestShouldReflect(unittest.TestCase):
    def test_reflects_when_current_key_moment_turn_not_marked(self):
        state = _ConversationState(turn_count=5)
        self.assertFalse(should_reflect(state, last_reflected_turn=5))
        self.assertTrue(should_reflect(state, last_reflected_turn=4))

    def test_skips_empty_or_initial_state(self):
        state = _ConversationState(turn_count=9)
        self.assertFalse(should_reflect(None, last_reflected_turn=0))
        self.assertFalse(should_reflect(_ConversationState(turn_count=0), last_reflected_turn=0))
        self.assertTrue(should_reflect(state, last_reflected_turn=8))

    def test_should_reflect_for_conversation_reads_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conversations_dir = Path(tmpdir)
            conversation_id = "conv-1"
            record_reflection_turn(conversations_dir, conversation_id, 5)
            self.assertFalse(
                should_reflect_for_conversation(
                    conversations_dir,
                    conversation_id,
                    _ConversationState(turn_count=5),
                )
            )
            self.assertTrue(
                should_reflect_for_conversation(
                    conversations_dir,
                    conversation_id,
                    _ConversationState(turn_count=6),
                )
            )


class TestReflectionParsing(unittest.TestCase):
    def test_reflector_prompt_mentions_recent_asset_interactions(self):
        self.assertIn("avoid repeating immediately", REFLECTOR_PROMPT)
        self.assertIn("already touched this recently", REFLECTOR_PROMPT)

    def test_reflector_prompt_mentions_direction_guidance(self):
        self.assertIn('"direction"', REFLECTOR_PROMPT)
        self.assertIn("durable work-direction guidance", REFLECTOR_PROMPT)
        self.assertIn("influence future planning", REFLECTOR_PROMPT)
        self.assertIn(
            "Ambient platform discoveries are evidence, not direction", REFLECTOR_PROMPT
        )

    def test_returns_none_when_reflector_hits_max_steps(self):
        self.assertIsNone(parse_reflection_result("Reached max steps."))

    def test_parses_valid_empty_reflection_payload(self):
        result = parse_reflection_result(
            '{"facts_to_store": [], "user_preferences": [], "daily_log_entries": []}'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.facts_to_store, [])
        self.assertEqual(result.user_preferences, [])
        self.assertEqual(result.daily_log_entries, [])

    def test_parses_direction_category(self):
        result = parse_reflection_result(
            """
            {
              "facts_to_store": [
                {
                  "text": "User wants the agent to focus on benchmarking next.",
                  "category": "direction",
                  "strength": 0.8,
                  "basis": "stated"
                }
              ],
              "user_preferences": [],
              "daily_log_entries": []
            }
            """
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.facts_to_store[0]["category"], "direction")
        self.assertEqual(result.facts_to_store[0]["strength"], 0.8)

    def test_parses_new_candidate_schema(self):
        result = parse_reflection_result(
            """
            {
              "candidates": [
                {
                  "text": "Asset abc was reviewed and should not be revisited immediately.",
                  "subject_type": "asset",
                  "category": "fact",
                  "basis": "observed",
                  "stability": "evolving",
                  "team_ids": ["team-1"],
                  "asset_ids": ["abc"],
                  "strength": 0.5,
                  "verification_hint": "check asset activity"
                }
              ],
              "user_preferences": [],
              "daily_log_entries": []
            }
            """
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.facts_to_store[0]["subject_type"], "asset")
        self.assertEqual(result.facts_to_store[0]["team_ids"], ["team-1"])
        self.assertEqual(result.facts_to_store[0]["asset_ids"], ["abc"])
        self.assertEqual(result.facts_to_store[0]["basis"], "observed")

    def test_parses_structured_daily_log_entries(self):
        result = parse_reflection_result(
            """
            {
              "candidates": [],
              "user_preferences": [],
              "daily_log_entries": [
                {"team_id": "team-materials", "entry": "[heartbeat] Reviewed materials feed"},
                {"team_id": "team-super", "entry": "[heartbeat] Checked superconductor thread"}
              ]
            }
            """
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            [(entry.team_id, entry.entry) for entry in result.daily_log_entries],
            [
                ("team-materials", "[heartbeat] Reviewed materials feed"),
                ("team-super", "[heartbeat] Checked superconductor thread"),
            ],
        )

    def test_validated_daily_log_entries_keep_distinct_team_entries(self):
        result = ReflectionResult(
            daily_log_entries=[
                DailyLogEntry(
                    team_id="team-materials", entry="[heartbeat] Materials work"
                ),
                DailyLogEntry(
                    team_id="team-super", entry="[heartbeat] Superconductor work"
                ),
                DailyLogEntry(team_id="unknown", entry="[heartbeat] Invalid team"),
                DailyLogEntry(
                    team_id="team-super", entry="[heartbeat] Superconductor work"
                ),
            ],
        )

        entries = validated_daily_log_entries(
            result,
            available_team_ids={"team-materials", "team-super"},
        )

        self.assertEqual(
            entries,
            [
                ("team-materials", "[heartbeat] Materials work"),
                ("team-super", "[heartbeat] Superconductor work"),
            ],
        )

    def test_validated_daily_log_entries_fallback_to_run_team_when_scoped(self):
        result = ReflectionResult(
            daily_log_entries=[
                DailyLogEntry(team_id="", entry="[heartbeat] Plan work"),
                DailyLogEntry(team_id="unknown", entry="[heartbeat] More plan work"),
            ],
        )

        entries = validated_daily_log_entries(
            result,
            run_team_id="team-plan",
            available_team_ids={"team-plan"},
        )

        self.assertEqual(
            entries,
            [
                ("team-plan", "[heartbeat] Plan work"),
                ("team-plan", "[heartbeat] More plan work"),
            ],
        )

    def test_build_run_reflection_task_mentions_redundant_follow_up_avoidance(self):
        task = build_run_reflection_task(
            task="Comment on a post if useful.",
            result="Left a comment on asset abc.",
            tool_summary=[
                {"tool": "ouro:create_comment", "result": "commented on asset abc"}
            ],
            run_mode="heartbeat",
        )

        self.assertIn("already touched recently", task)
        self.assertIn("avoid redundant follow-up", task)

    def test_build_run_reflection_task_mentions_direction_feedback(self):
        task = build_run_reflection_task(
            task="Comment from alice: please focus more on dataset quality.",
            result="Acknowledged and replied.",
            tool_summary=[{"tool": "ouro:create_comment", "result": "replied"}],
            run_mode="autonomous",
            event_type="comment",
        )

        self.assertIn('category="direction"', task)
        self.assertIn("comments, mentions, plan-review feedback", task)

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
                        "category": "preference",
                        "basis": "stated",
                        "strength": 0.7,
                    }
                ],
                user_preferences=[],
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
            self.assertIs(backend.items[0]["infer"], False)
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
                        "subject_type": "agent",
                        "category": "fact",
                        "basis": "observed",
                        "strength": 0.8,
                    }
                ],
                user_preferences=[],
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

    def test_apply_reflection_preserves_direction_category(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            conversations_dir = workspace / "conversations"
            backend = _FakeMemoryBackend()
            result = ReflectionResult(
                facts_to_store=[
                    {
                        "text": "User wants the agent to stop posting low-signal updates.",
                        "category": "direction",
                        "basis": "stated",
                        "strength": 0.8,
                    }
                ],
                user_preferences=[],
            )

            apply_reflection(
                result,
                backend,
                agent_id="hermes",
                user_id="user-1",
                conversation_id="conv-1",
                workspace=workspace,
                conversations_dir=conversations_dir,
                conversation_state=_ConversationState(turn_count=3),
            )

            self.assertEqual(backend.items[0]["metadata"]["category"], "direction")


if __name__ == "__main__":
    unittest.main()
