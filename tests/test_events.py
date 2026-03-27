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

    ouro_package = types.ModuleType("ouro")
    ouro_package.__path__ = []
    sys.modules["ouro"] = ouro_package

    ouro_events = types.ModuleType("ouro.events")

    @dataclass(frozen=True)
    class WebhookEvent:
        event_type: str
        data: dict
        timestamp: str | None
        recipient_user_id: str | None
        conversation_id: str | None
        actor_user_id: str | None
        sender_username: str | None
        source_id: str | None
        source_asset_type: str | None

    def parse_webhook_event(body):
        data = body.get("data", {})
        event_type = body["event"].strip().lower().replace("_", "-")
        return WebhookEvent(
            event_type=event_type,
            data=data,
            timestamp=body.get("timestamp"),
            recipient_user_id=body.get("user_id"),
            conversation_id=data.get("conversation_id"),
            actor_user_id=data.get("user_id"),
            sender_username=data.get("sender_username") or data.get("sender"),
            source_id=data.get("source_id"),
            source_asset_type=data.get("source_asset_type"),
        )

    ouro_events.WebhookEvent = WebhookEvent
    ouro_events.parse_webhook_event = parse_webhook_event
    sys.modules["ouro.events"] = ouro_events

    config_module = types.ModuleType("ouro_agents.config")

    class RunMode(str, Enum):
        CHAT = "chat"
        CHAT_REPLY = "chat-reply"
        AUTONOMOUS = "autonomous"
        HEARTBEAT = "heartbeat"
        PLAN = "plan"
        REVIEW = "review"

    config_module.RunMode = RunMode
    sys.modules["ouro_agents.config"] = config_module

    provenance_module = types.ModuleType("ouro_agents.provenance")

    @dataclass(frozen=True)
    class AssetProvenance:
        is_own_asset: bool = False
        in_planning_space: bool = False
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


class TestBuildEventRunContext(unittest.TestCase):
    def test_top_level_comment_prefetches_post_and_all_comments(self):
        """Top-level comment on a post: load the post + all top-level comments."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "source_id": "comment-456",
                    "source_asset_type": "comment",
                    "target_id": "asset-123",
                    "target_asset_type": "post",
                    "text": "What do you think?",
                },
            }
        )

        self.assertEqual(event_run.prefetch.asset_ids, ["asset-123"])
        self.assertEqual(event_run.prefetch.comment_parent_ids, ["asset-123"])
        self.assertEqual(event_run.prefetch.thread_comment_parent_ids, [])
        self.assertEqual(event_run.reply_parent_id, "comment-456")
        self.assertEqual(event_run.thread_parent_id, "asset-123")
        self.assertEqual(event_run.feedback_text, "What do you think?")

    def test_thread_reply_prefetches_post_comments_and_thread(self):
        """Thread reply: load the post, all top-level comments, AND the thread."""
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "source_id": "comment-789",
                    "source_asset_type": "comment",
                    "target_id": "thread-123",
                    "target_asset_type": "comment",
                    "focus_asset_id": "plan-post-1",
                    "focus_asset_type": "post",
                    "text": "Can we tighten the scope?",
                },
            }
        )

        self.assertEqual(event_run.prefetch.asset_ids, ["plan-post-1"])
        self.assertEqual(event_run.prefetch.comment_parent_ids, ["plan-post-1"])
        self.assertEqual(event_run.prefetch.thread_comment_parent_ids, ["thread-123"])
        self.assertIn("post (id: plan-post-1)", event_run.task)
        self.assertIn("create_comment on `comment-789`", event_run.task)
        self.assertEqual(event_run.reply_parent_id, "comment-789")
        self.assertEqual(event_run.thread_parent_id, "thread-123")
        self.assertEqual(event_run.feedback_text, "Can we tighten the scope?")


if __name__ == "__main__":
    unittest.main()
