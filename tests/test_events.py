import unittest
import importlib.util
import sys
import types
from pathlib import Path
from dataclasses import dataclass
from enum import Enum


def _load_events_module():
    repo_root = Path(__file__).resolve().parents[2]
    package_dir = repo_root / "ouro-agents" / "ouro_agents"

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
        AUTONOMOUS = "autonomous"
        HEARTBEAT = "heartbeat"

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

    spec = importlib.util.spec_from_file_location(
        "ouro_agents.events",
        package_dir / "events.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.events"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


build_event_run_context = _load_events_module().build_event_run_context


class TestBuildEventRunContext(unittest.TestCase):
    def test_comment_event_populates_source_asset_ref(self):
        event_run = build_event_run_context(
            {
                "event": "comment",
                "user_id": "recipient-1",
                "data": {
                    "user_id": "actor-1",
                    "source_id": "asset-123",
                    "source_asset_type": "post",
                    "text": "What do you think?",
                },
            }
        )

        self.assertEqual(event_run.asset_refs, ("asset-123",))


if __name__ == "__main__":
    unittest.main()
