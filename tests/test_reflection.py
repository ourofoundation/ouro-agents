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
store_reflection_memories = _reflection_module.store_reflection_memories
validated_daily_log_entries = _reflection_module.validated_daily_log_entries


class _FakeMemoryBackend:
    def __init__(self):
        self.items = []
        self.deleted = []

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

    def delete(self, memory_id):
        self.deleted.append(memory_id)


class TestReflectionParsing(unittest.TestCase):
    def test_reflector_prompt_has_distinct_optional_friction_contract(self):
        self.assertIn('"friction":', REFLECTOR_PROMPT)
        self.assertIn("Most runs", REFLECTOR_PROMPT)
        self.assertIn("Do NOT turn friction into candidates", REFLECTOR_PROMPT)

    def test_parses_valid_friction_and_skips_malformed_items(self):
        result = parse_reflection_result(
            """
            {
              "candidates": [],
              "user_preferences": [],
              "daily_log_entries": [],
              "friction": [
                {
                  "kind": "skill_misled",
                  "skill": "publishing",
                  "evidence": "The skill prescribed a removed argument.",
                  "severity": "high"
                },
                {"kind": "not-real", "evidence": "ignore this"},
                {"kind": "tool_failure", "severity": "low"},
                "also ignore this"
              ]
            }
            """
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result.friction,
            [
                {
                    "kind": "skill_misled",
                    "skill": "publishing",
                    "evidence": "The skill prescribed a removed argument.",
                    "severity": "high",
                }
            ],
        )

    def test_old_reflection_payload_defaults_to_no_friction(self):
        result = parse_reflection_result(
            '{"candidates": [], "user_preferences": [], "daily_log_entries": []}'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.friction, [])

    def test_reflector_prompt_mentions_coil_adoption(self):
        self.assertIn("Coil adoption", REFLECTOR_PROMPT)
        self.assertIn("run_coil", REFLECTOR_PROMPT)
        self.assertIn("owning workspace skill", REFLECTOR_PROMPT)
        self.assertIn("addendum.md", REFLECTOR_PROMPT)

    def test_run_reflection_task_mentions_coil_skill_adoption(self):
        from ouro_agents.subagents.reflector import build_run_reflection_task

        task = build_run_reflection_task(
            task="author coils",
            result="wrote coils/outreach-triage",
            tool_summary=[{"tool": "run_coil", "result": "{}"}],
            run_mode="chat",
        )
        self.assertIn("owning workspace skill", task)
        self.assertIn("addendum.md", task)
        self.assertIn("run_coil", task)

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
        self.assertIn("NO_ACTION is only an immediate reply", REFLECTOR_PROMPT)
        self.assertIn("decision; it must not cause you to discard", REFLECTOR_PROMPT)

    def test_returns_none_when_reflector_hits_max_steps(self):
        self.assertIsNone(parse_reflection_result("Reached max steps."))

    def test_parses_json_wrapped_in_prose(self):
        result = parse_reflection_result(
            "Here is my reflection on this completed run:\n\n"
            "**What happened:** hermes fixed mention syntax.\n\n"
            '{"candidates": [{"text": "Ouro mentions use plain @username.", '
            '"category": "preference", "strength": 0.8}], '
            '"user_preferences": [], "daily_log_entries": []}\n\n'
            "That's everything worth keeping."
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.facts_to_store), 1)
        self.assertEqual(result.facts_to_store[0]["category"], "preference")

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

    def test_parses_word_strength_scale(self):
        result = parse_reflection_result(
            '{"candidates": [{"text": "Fact one.", "strength": "high"}, '
            '{"text": "Fact two.", "strength": "minor"}, '
            '{"text": "Fact three.", "strength": 0.5}], '
            '"user_preferences": [], "daily_log_entries": []}'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            [fact["strength"] for fact in result.facts_to_store],
            [0.8, 0.3, 0.5],
        )

    def test_parses_supersedes_ids(self):
        result = parse_reflection_result(
            '{"candidates": [{"text": "As of 2026-07-04, X is banned.", '
            '"category": "direction", "strength": "high", '
            '"supersedes": ["mem-old", " ", "mem-stale"]}], '
            '"user_preferences": [], "daily_log_entries": []}'
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result.facts_to_store[0]["supersedes"], ["mem-old", "mem-stale"]
        )

    def test_build_run_reflection_task_mentions_redundant_follow_up_avoidance(self):
        task = build_run_reflection_task(
            task="Comment on a post if useful.",
            result="Left a comment on asset abc.",
            tool_summary=[
                {"tool": "ouro:write_comment", "result": "commented on asset abc"}
            ],
            run_mode="heartbeat",
        )

        self.assertIn("already touched recently", task)
        self.assertIn("avoid redundant follow-up", task)

    def test_build_run_reflection_task_mentions_direction_feedback(self):
        task = build_run_reflection_task(
            task="Comment from alice: please focus more on dataset quality.",
            result="NO_ACTION",
            tool_summary=[{"tool": "ouro:write_comment", "result": "replied"}],
            run_mode="autonomous",
            event_type="comment",
        )

        self.assertIn('category="direction"', task)
        self.assertIn("even when the run result is NO_ACTION", task)
        self.assertIn("comments, mentions, plan-review feedback", task)

    def test_build_run_reflection_task_includes_memory_notes_and_produced_assets(self):
        task = build_run_reflection_task(
            task="Publish coverage map.",
            result="Created dataset 019f5902-b1eb-7794-b3c9-ada8acfe9d36",
            tool_summary=[
                {
                    "tool": "ouro:create_dataset",
                    "result": "id=019f5902-b1eb-7794-b3c9-ada8acfe9d36",
                }
            ],
            run_mode="heartbeat",
            memory_notes=[
                "Oliynyk coverage map is <new_dataset_id> in permanent-magnets",
            ],
        )

        self.assertIn("Preflight memory notes", task)
        self.assertIn("<new_dataset_id>", task)
        self.assertIn("created a durable Ouro asset", task)
        self.assertIn("asset_ids", task)

    def test_build_run_reflection_task_filters_noisy_tools_keeps_order(self):
        task = build_run_reflection_task(
            task="Review feed and respond where helpful.",
            result="Commented twice and updated a post.",
            tool_summary=[
                {"tool": "memory_recall", "result": "prior context"},
                {"tool": "ouro:get_asset", "result": "asset body"},
                {"tool": "ouro:write_comment", "result": "comment one"},
                {"tool": "load_tool", "result": "loaded search"},
                {"tool": "ouro:update_post", "result": "updated plan post"},
                {"tool": "ouro:write_comment", "result": "comment two"},
            ],
            run_mode="heartbeat",
        )

        self.assertNotIn("memory_recall", task)
        self.assertNotIn("ouro:get_asset", task)
        self.assertNotIn("load_tool", task)
        self.assertIn("- ouro:write_comment: comment one", task)
        self.assertIn("- ouro:update_post: updated plan post", task)
        self.assertIn("- ouro:write_comment: comment two", task)
        self.assertLess(
            task.index("- ouro:write_comment: comment one"),
            task.index("- ouro:update_post: updated plan post"),
        )
        self.assertLess(
            task.index("- ouro:update_post: updated plan post"),
            task.index("- ouro:write_comment: comment two"),
        )

    def test_build_run_reflection_task_includes_all_non_noisy_tools(self):
        tool_summary = [
            {"tool": "ouro:write_comment", "result": f"comment {i}"} for i in range(12)
        ]
        task = build_run_reflection_task(
            task="Comment when appropriate.",
            result="Left many comments.",
            tool_summary=tool_summary,
            run_mode="heartbeat",
        )

        self.assertIn("- ouro:write_comment: comment 0", task)
        self.assertIn("- ouro:write_comment: comment 11", task)

    def test_build_run_reflection_task_includes_process_signals(self):
        task = build_run_reflection_task(
            task="Complete the task.",
            result="Done.",
            run_mode="autonomous",
            step_count=9,
            retry_error_count=2,
            loaded_skill_names=["publishing", "filesystem", "publishing"],
        )

        self.assertIn("Process signals:", task)
        self.assertIn("- Main-agent steps: 9", task)
        self.assertIn("- Retry/error steps: 2", task)
        self.assertIn("- Loaded skills: publishing, filesystem", task)


class TestApplyReflection(unittest.TestCase):
    def test_apply_reflection_enqueues_friction_with_run_context(self):
        from ouro_agents.memory.friction import FrictionQueue

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            result = ReflectionResult(
                friction=[
                    {
                        "kind": "user_correction",
                        "skill": None,
                        "evidence": "The user corrected the requested output format.",
                        "severity": "high",
                    }
                ]
            )

            apply_reflection(
                result,
                _FakeMemoryBackend(),
                agent_id="hermes",
                user_id="user-1",
                conversation_id="conv-1",
                run_id="run-1",
                workspace=workspace,
                conversations_dir=workspace / "conversations",
                team_id="team-42",
                mode="chat",
            )

            rows = FrictionQueue.for_workspace(workspace).pending()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].run_id, "run-1")
            self.assertEqual(rows[0].mode, "chat")
            self.assertEqual(rows[0].team_id, "team-42")
            self.assertEqual(rows[0].kind.value, "user_correction")

    def test_valid_reflection_stores_fact(self):
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
            )

            self.assertEqual(len(backend.items), 1)
            self.assertEqual(backend.items[0]["text"], "User prefers concise updates.")
            self.assertIs(backend.items[0]["infer"], False)

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
                team_id="team-42",
            )

            self.assertEqual(backend.items[0]["team_id"], "team-42")

    def test_store_reflection_memories_retires_superseded_ids(self):
        backend = _FakeMemoryBackend()
        result = ReflectionResult(
            facts_to_store=[
                {
                    "text": "As of 2026-07-04, alice banned auto-posting benchmarks.",
                    "category": "direction",
                    "basis": "stated",
                    "strength": 0.8,
                    "supersedes": ["mem-old-advice"],
                }
            ],
        )

        stored = store_reflection_memories(
            result,
            backend,
            agent_id="hermes",
            user_id="user-1",
            run_id="run-1",
        )

        self.assertEqual(stored, 1)
        self.assertEqual(backend.deleted, ["mem-old-advice"])

    def test_store_reflection_memories_skips_supersedes_when_store_fails(self):
        class _FailingBackend(_FakeMemoryBackend):
            def add(self, *args, **kwargs):
                raise RuntimeError("store unavailable")

        backend = _FailingBackend()
        result = ReflectionResult(
            facts_to_store=[
                {
                    "text": "As of 2026-07-04, alice banned auto-posting benchmarks.",
                    "category": "direction",
                    "supersedes": ["mem-old-advice"],
                }
            ],
        )

        stored = store_reflection_memories(
            result,
            backend,
            agent_id="hermes",
            user_id="user-1",
            run_id="run-1",
        )

        self.assertEqual(stored, 0)
        self.assertEqual(backend.deleted, [])

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
            )

            self.assertEqual(backend.items[0]["metadata"]["category"], "direction")


class _SearchableFakeBackend(_FakeMemoryBackend):
    def __init__(self, existing_texts=None):
        super().__init__()
        self.existing_texts = existing_texts or []

    def search(self, query, agent_id, **kwargs):
        return [
            types.SimpleNamespace(id=f"existing-{i}", text=text)
            for i, text in enumerate(self.existing_texts)
        ]


def _candidate(text, strength=0.5, supersedes=None):
    return {
        "text": text,
        "subject_type": "agent",
        "subject_id_hint": "self",
        "category": "fact",
        "basis": "observed",
        "stability": "stable",
        "team_ids": [],
        "asset_ids": [],
        "strength": strength,
        "supersedes": supersedes or [],
    }


class TestReflectionDeduplication(unittest.TestCase):
    def _store(self, candidates, backend):
        result = ReflectionResult(facts_to_store=candidates)
        return store_reflection_memories(
            result,
            backend,
            agent_id="agent-1",
            user_id="user-1",
            run_id="run-1",
        )

    def test_batch_near_duplicates_collapse_to_strongest(self):
        backend = _SearchableFakeBackend()
        stored = self._store(
            [
                _candidate(
                    "C15 MgCu2 Laves phase survives Orb v3 relaxation with symmetry intact",
                    strength=0.5,
                ),
                _candidate(
                    "C15 MgCu2 Laves phase survives Orb v3 relaxation with symmetry intact,"
                    " distinguishing C14 from C15 under MLIP relaxation",
                    strength=0.8,
                    supersedes=["old-1"],
                ),
            ],
            backend,
        )
        self.assertEqual(stored, 1)
        self.assertIn("distinguishing", backend.items[0]["text"])
        self.assertEqual(backend.deleted, ["old-1"])

    def test_distinct_candidates_are_kept(self):
        backend = _SearchableFakeBackend()
        stored = self._store(
            [
                _candidate("alice prefers benchmark results in the eval-lab team"),
                _candidate("the alloy-corpus dataset stores formation energy in eV/atom"),
            ],
            backend,
        )
        self.assertEqual(stored, 2)

    def test_over_extraction_capped_at_max(self):
        max_memories = _reflection_module.MAX_REFLECTION_MEMORIES
        backend = _SearchableFakeBackend()
        distinct_texts = [
            "alice prefers dark mode in every editor she uses",
            "the staging database runs postgres seventeen on port 5433",
            "hermes reviews pull requests every morning before standup",
            "the eval-lab team publishes weekly benchmark summaries on fridays",
            "orb v3 relaxation requires ICSD-anchored input geometries",
            "deployment to modal needs the org-scoped access token",
            "quest entries on closable quests allow one submission per item",
            "the chemistry team feed surfaces spinel oxide discussion threads",
        ]
        candidates = [
            _candidate(text, strength=0.1 * (i + 1))
            for i, text in enumerate(distinct_texts[: max_memories + 3])
        ]
        stored = self._store(candidates, backend)
        self.assertEqual(stored, max_memories)
        # The strongest candidates survive the cap.
        texts = [item["text"] for item in backend.items]
        self.assertIn(distinct_texts[max_memories + 2], texts)

    def test_cross_run_duplicate_skipped(self):
        backend = _SearchableFakeBackend(
            existing_texts=[
                "spglib does not have a get_spacegroup_number function; use"
                " get_symmetry_dataset()['number'] instead"
            ]
        )
        stored = self._store(
            [
                _candidate(
                    "spglib does not have a get_spacegroup_number function, use"
                    " get_symmetry_dataset()['number'] instead"
                )
            ],
            backend,
        )
        self.assertEqual(stored, 0)

    def test_supersedes_bypasses_cross_run_duplicate_check(self):
        backend = _SearchableFakeBackend(
            existing_texts=["the agreed build priority order is mCGCNN first"]
        )
        stored = self._store(
            [
                _candidate(
                    "the agreed build priority order is mCGCNN first",
                    supersedes=["existing-0"],
                )
            ],
            backend,
        )
        self.assertEqual(stored, 1)
        self.assertEqual(backend.deleted, ["existing-0"])


if __name__ == "__main__":
    unittest.main()
