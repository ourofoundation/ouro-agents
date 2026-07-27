import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _load_events_module():
    repo_root = Path(__file__).resolve().parents[2]
    package_dir = repo_root / "ouro-agents" / "ouro_agents"
    original_config = sys.modules.get("ouro_agents.config")
    original_provenance = sys.modules.get("ouro_agents.provenance")

    if "ouro_agents" not in sys.modules:
        package = types.ModuleType("ouro_agents")
        package.__path__ = [str(package_dir)]
        sys.modules["ouro_agents"] = package

    # Import the real ouro.events from the in-repo `ouro-py` package; it is
    # the canonical source of `WebhookEvent` parsing and parity-tested against
    # the ouro-js registry.
    import ouro.events  # noqa: F401

    config_module = types.ModuleType("ouro_agents.config")

    class RunMode(str, Enum):
        CHAT = "chat"
        AUTONOMOUS = "autonomous"
        HEARTBEAT = "heartbeat"
        PLAN = "plan"

    config_module.RunMode = RunMode
    sys.modules["ouro_agents.config"] = config_module

    provenance_module = types.ModuleType("ouro_agents.provenance")

    @dataclass(frozen=True)
    class AssetProvenance:
        is_own_asset: bool = False
        team_id: str | None = None
        plan_cycle: object | None = None

        @property
        def is_plan_feedback(self) -> bool:
            return False

        @property
        def is_historical_plan_feedback(self) -> bool:
            return False

    provenance_module.AssetProvenance = AssetProvenance
    sys.modules["ouro_agents.provenance"] = provenance_module

    # Stub the artifacts module so events.py can import PrefetchSpec
    artifacts_spec = importlib.util.spec_from_file_location(
        "ouro_agents.artifacts",
        package_dir / "artifacts.py",
    )
    artifacts_module = importlib.util.module_from_spec(artifacts_spec)
    sys.modules["ouro_agents.artifacts"] = artifacts_module
    assert artifacts_spec and artifacts_spec.loader

    # artifacts.py imports from .constants — stub it
    constants_spec = importlib.util.spec_from_file_location(
        "ouro_agents.constants",
        package_dir / "constants.py",
    )
    constants_module = importlib.util.module_from_spec(constants_spec)
    sys.modules["ouro_agents.constants"] = constants_module
    assert constants_spec and constants_spec.loader
    constants_spec.loader.exec_module(constants_module)

    artifacts_spec.loader.exec_module(artifacts_module)

    spec = importlib.util.spec_from_file_location(
        "ouro_agents.events",
        package_dir / "events.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.events"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    if original_config is not None:
        sys.modules["ouro_agents.config"] = original_config
    else:
        sys.modules.pop("ouro_agents.config", None)
    if original_provenance is not None:
        sys.modules["ouro_agents.provenance"] = original_provenance
    else:
        sys.modules.pop("ouro_agents.provenance", None)
    return module


build_event_run_context = _load_events_module().build_event_run_context
EventSurface = sys.modules["ouro_agents.security.policy"].EventSurface


class TestBuildEventRunContext(unittest.TestCase):
    def test_new_message_uses_top_level_user_id(self):
        event_run = build_event_run_context(
            {
                "event": "new-message",
                "timestamp": "2026-04-30T22:00:00Z",
                "user_id": "agent-recipient",
                "data": {
                    "user_id": "human-actor",
                    "user": {
                        "id": "human-actor",
                        "username": "alice",
                        "is_agent": False,
                    },
                    "conversation_id": "conv-1",
                    "text": "Hello",
                    "type": "message",
                },
            }
        )

        self.assertEqual(event_run.user_id, "human-actor")
        self.assertEqual(event_run.actor_user_id, "human-actor")
        self.assertEqual(event_run.actor_username, "alice")
        self.assertFalse(event_run.actor_is_agent)
        self.assertEqual(event_run.event_text, "Hello")
        self.assertEqual(event_run.received_at, "2026-04-30T22:00:00Z")
        self.assertIn("New conversation message from alice", event_run.task)

    def test_new_message_falls_back_to_nested_user(self):
        event_run = build_event_run_context(
            {
                "event": "new-message",
                "user_id": "agent-recipient",
                "data": {
                    "user": {
                        "id": "human-actor",
                        "username": "alice",
                        "is_agent": False,
                    },
                    "conversation_id": "conv-1",
                    "text": "Hello",
                    "type": "message",
                },
            }
        )

        self.assertEqual(event_run.user_id, "human-actor")
        self.assertEqual(event_run.actor_user_id, "human-actor")
        self.assertIn("New conversation message from alice", event_run.task)

    def test_new_message_uses_sender_object(self):
        event_run = build_event_run_context(
            {
                "event": "new-message",
                "user_id": "agent-recipient",
                "data": {
                    "sender": {
                        "id": "human-actor",
                        "username": "alice",
                        "is_agent": False,
                    },
                    "conversation_id": "conv-1",
                    "text": "Hello",
                    "type": "message",
                },
            }
        )

        self.assertEqual(event_run.user_id, "human-actor")
        self.assertEqual(event_run.actor_user_id, "human-actor")
        self.assertIn("New conversation message from alice", event_run.task)

    def test_top_level_comment_prefetches_post_and_all_comments(self):
        """Top-level comment on a post: load the post + all top-level comments."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "alice", "is_agent": False},
                    "source_id": "comment-456",
                    "source_asset_type": "comment",
                    "target_id": "asset-123",
                    "target_asset_type": "post",
                    "root_asset_id": "asset-123",
                    "root_asset_type": "post",
                    "text": "What do you think?",
                    "team": {"id": "team-1", "name": "research"},
                    "organization": {"id": "org-1", "name": "Acme"},
                },
            }
        )

        self.assertEqual(event_run.prefetch.asset_ids, ["asset-123"])
        self.assertEqual(event_run.prefetch.comment_parent_ids, ["asset-123"])
        self.assertEqual(event_run.prefetch.thread_comment_parent_ids, [])
        self.assertEqual(event_run.prefetch.focus_comment_id, "comment-456")
        self.assertEqual(event_run.prefetch.focus_comment_author, "alice")
        self.assertEqual(event_run.prefetch.focus_comment_text, "What do you think?")
        self.assertEqual(event_run.reply_parent_id, "comment-456")
        self.assertEqual(event_run.thread_parent_id, "asset-123")
        self.assertEqual(event_run.feedback_text, "What do you think?")
        self.assertEqual(event_run.actor_user_id, "actor-1")
        self.assertEqual(event_run.actor_username, "alice")
        self.assertFalse(event_run.actor_is_agent)
        self.assertEqual(event_run.event_text, "What do you think?")
        self.assertEqual(event_run.root_asset_id, "asset-123")
        self.assertEqual(event_run.root_asset_type, "post")
        self.assertEqual(event_run.surface, EventSurface.COMMENT)
        self.assertIn("Untrusted Evidence: triggering comment", event_run.task)
        self.assertIn("<untrusted-evidence>", event_run.task)

    def test_comment_carries_notification_ids(self):
        event_run = build_event_run_context(
            {
                "event": "mention",
                "user_id": "recipient-1",
                "notification_id": "notification-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "alice", "is_agent": False},
                    "source_id": "comment-456",
                    "source_asset_type": "comment",
                    "target_id": "asset-123",
                    "target_asset_type": "post",
                    "root_asset_id": "asset-123",
                    "root_asset_type": "post",
                    "text": "@agent what do you think?",
                    "notification_id": "notification-1",
                },
            }
        )

        self.assertEqual(event_run.notification_ids, ("notification-1",))

    def test_thread_reply_prefetches_post_comments_and_thread(self):
        """Thread reply: load the post, all top-level comments, AND the thread."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "alice", "is_agent": False},
                    "source_id": "comment-789",
                    "source_asset_type": "comment",
                    "target_id": "thread-123",
                    "target_asset_type": "comment",
                    "root_asset_id": "plan-post-1",
                    "root_asset_type": "post",
                    "text": "Can we tighten the scope?",
                    "team": None,
                    "organization": None,
                },
            }
        )

        self.assertEqual(event_run.prefetch.asset_ids, ["plan-post-1"])
        self.assertEqual(event_run.prefetch.comment_parent_ids, ["plan-post-1"])
        self.assertEqual(event_run.prefetch.thread_comment_parent_ids, ["thread-123"])
        self.assertEqual(event_run.prefetch.focus_comment_id, "comment-789")
        self.assertEqual(event_run.prefetch.focus_comment_author, "alice")
        self.assertEqual(event_run.prefetch.focus_comment_text, "Can we tighten the scope?")
        self.assertIn("post (id: plan-post-1)", event_run.task)
        self.assertIn("`write_comment` on `comment-789`", event_run.task)
        self.assertEqual(event_run.reply_parent_id, "comment-789")
        self.assertEqual(event_run.thread_parent_id, "thread-123")
        self.assertEqual(event_run.feedback_text, "Can we tighten the scope?")
        self.assertEqual(event_run.actor_user_id, "actor-1")
        self.assertEqual(event_run.root_asset_id, "plan-post-1")

    def test_mention_reply_uses_parent_asset_id_for_thread_context(self):
        """Mention reply context uses the comment's parent asset."""
        event_run = build_event_run_context(
            {
                "event": "mention",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "alice", "is_agent": False},
                    "source_id": "comment-789",
                    "source_asset_type": "comment",
                    "target_id": "mentioned-user-1",
                    "target_asset_type": "user",
                    "parent_asset_id": "thread-123",
                    "root_asset_id": "plan-post-1",
                    "root_asset_type": "post",
                    "text": "@agent can we tighten the scope?",
                },
            }
        )

        self.assertEqual(event_run.prefetch.asset_ids, ["plan-post-1"])
        self.assertEqual(event_run.prefetch.comment_parent_ids, ["plan-post-1"])
        self.assertEqual(event_run.prefetch.thread_comment_parent_ids, ["thread-123"])
        self.assertEqual(event_run.thread_parent_id, "thread-123")
        self.assertIn("current thread", event_run.task)

    def test_mention_without_root_does_not_use_mentioned_user_as_root(self):
        event_run = build_event_run_context(
            {
                "event": "mention",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "alice", "is_agent": False},
                    "source_id": "comment-789",
                    "source_asset_type": "comment",
                    "target_id": "mentioned-user-1",
                    "target_asset_type": "user",
                    "parent_asset_id": "thread-123",
                    "parent_asset_type": "comment",
                    "text": "@agent can we tighten the scope?",
                },
            }
        )

        self.assertEqual(event_run.root_asset_id, "thread-123")
        self.assertEqual(event_run.root_asset_type, "comment")
        self.assertEqual(event_run.thread_parent_id, "thread-123")
        self.assertEqual(event_run.prefetch.thread_comment_parent_ids, [])
        self.assertNotIn("mentioned-user-1", event_run.prefetch.asset_ids)

    def test_comment_task_includes_no_action_guidance(self):
        """Comment tasks should include strong NO_ACTION decision framing."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "bob", "is_agent": False},
                    "source_id": "comment-100",
                    "source_asset_type": "comment",
                    "target_id": "post-1",
                    "target_asset_type": "post",
                    "root_asset_id": "post-1",
                    "root_asset_type": "post",
                    "text": "Looks good!",
                },
            }
        )

        self.assertIn("Decision: Respond or Do Nothing", event_run.task)
        self.assertIn("NO_ACTION", event_run.task)
        self.assertIn("acknowledgment", event_run.task)

    def test_thread_reply_includes_thread_caution(self):
        """Thread replies should include extra caution about back-and-forth."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "carol", "is_agent": False},
                    "source_id": "comment-200",
                    "source_asset_type": "comment",
                    "target_id": "comment-100",
                    "target_asset_type": "comment",
                    "root_asset_id": "post-1",
                    "root_asset_type": "post",
                    "text": "Agreed, that makes sense.",
                },
            }
        )

        self.assertIn("Thread reply caution", event_run.task)
        self.assertIn("let the thread end", event_run.task)

    def test_mention_uses_direct_request_framing(self):
        """Mentions are a direct summons, not ambient conversation."""
        event_run = build_event_run_context(
            {
                "event": "mention",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "eve", "is_agent": False},
                    "source_id": "comment-400",
                    "source_asset_type": "comment",
                    "root_asset_id": "post-1",
                    "root_asset_type": "post",
                    "text": "@agent write a summary post please",
                },
            }
        )

        self.assertIn("mentioned by name", event_run.task)
        self.assertNotIn("Decision: Respond or Do Nothing", event_run.task)
        self.assertIn("no closing offers", event_run.task)

    def test_unknown_source_id_falls_back_to_root_for_reply(self):
        """Never instruct write_comment on the literal id 'unknown'."""
        event_run = build_event_run_context(
            {
                "event": "mention",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "frank", "is_agent": False},
                    "root_asset_id": "post-9",
                    "root_asset_type": "post",
                    "text": "",
                },
            }
        )

        self.assertIn("`write_comment` on `post-9`", event_run.task)
        self.assertNotIn("`unknown`", event_run.task)
        # Empty comment text: no empty evidence block, point at the asset body.
        self.assertIn("the request is in the asset content", event_run.task)
        self.assertNotIn("comment_id: unknown", event_run.task)

    def test_top_level_comment_omits_thread_caution(self):
        """Top-level comments should NOT have thread-specific caution."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "dave", "is_agent": False},
                    "source_id": "comment-300",
                    "source_asset_type": "comment",
                    "target_id": "post-1",
                    "target_asset_type": "post",
                    "root_asset_id": "post-1",
                    "root_asset_type": "post",
                    "text": "What about X?",
                },
            }
        )

        self.assertNotIn("Thread reply caution", event_run.task)

    def test_quest_comment_includes_item_hygiene_guidance(self):
        """Comments on quests remind the agent to close the item loop."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {
                        "id": "actor-1",
                        "username": "matt",
                        "is_agent": False,
                    },
                    "source_id": "comment-approval",
                    "source_asset_type": "comment",
                    "target_id": "draft-comment-1",
                    "target_asset_type": "comment",
                    "root_asset_id": "quest-1",
                    "root_asset_type": "quest",
                    "text": "Good to send!",
                },
            }
        )

        self.assertIn("## Quest items", event_run.task)
        self.assertIn("waiting_on", event_run.task)
        self.assertIn("update_quest_item", event_run.task)
        self.assertIn("complete_quest_item", event_run.task)
        self.assertIn("skipped", event_run.task)
        self.assertIn("ouro:update_quest_item", event_run.preload_tools)
        self.assertIn("ouro:complete_quest_item", event_run.preload_tools)
        self.assertEqual(event_run.mode.value, "autonomous")

    def test_post_comment_omits_quest_item_hygiene_guidance(self):
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "user": {"id": "actor-1", "username": "bob", "is_agent": False},
                    "source_id": "comment-100",
                    "source_asset_type": "comment",
                    "root_asset_id": "post-1",
                    "root_asset_type": "post",
                    "text": "Looks good!",
                },
            }
        )

        self.assertNotIn("## Quest items", event_run.task)
        self.assertNotIn("complete_quest_item", event_run.task)


if __name__ == "__main__":
    unittest.main()
