"""Tests for the heartbeat Notification Inbox builder."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import NAMESPACE_URL, uuid5

from ouro.models import Notification, Page

from ouro_agents.config import (
    EventDeliveryConfig,
    NotificationInboxConfig,
    OuroAgentsConfig,
)
from ouro_agents.notification_inbox import (
    InboxThread,
    build_notification_inbox,
    expire_stale,
    fetch_unread,
    group_threads,
    mark_replied_notifications_read,
    render_inbox,
    thread_key_for,
    wrap_write_comment_with_mark_read,
)


def _uid(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, name))


def _notif(
    *,
    nid: str,
    notif_type: str = "comment",
    text: str = "hello",
    created_at: datetime | None = None,
    parent_asset_id: str | None = "thread-1",
    asset_id: str | None = "post-1",
    asset_name: str = "GPSK-300 results",
    asset_type: str = "post",
    username: str = "apollo",
    actor_type: str = "agent",
    viewed: bool = False,
) -> Notification:
    content: dict = {"text": text}
    if parent_asset_id is not None:
        content["parent"] = {"assetId": parent_asset_id}
    if asset_id is not None:
        content["asset"] = {
            "assetId": asset_id,
            "id": asset_id,
            "name": asset_name,
            "asset_type": asset_type,
        }
    return Notification.model_validate(
        {
            "id": _uid(nid),
            "type": notif_type,
            "viewed": viewed,
            "created_at": created_at
            or datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
            "content": content,
            "source_user": {
                "user_id": _uid(username),
                "username": username,
                "actor_type": actor_type,
            },
            "asset": {
                "id": _uid(asset_id or parent_asset_id or nid),
                "name": asset_name,
                "asset_type": asset_type,
            },
            "asset_id": _uid(asset_id) if asset_id else None,
        }
    )


class TestEventDeliveryConfig(unittest.TestCase):
    def test_defaults_to_realtime(self):
        cfg = EventDeliveryConfig()
        self.assertEqual(cfg.mode_for("comment"), "realtime")
        self.assertEqual(cfg.mode_for("mention"), "realtime")
        self.assertEqual(cfg.mode_for("new-message"), "realtime")

    def test_heartbeat_mode_for_configured_events(self):
        cfg = EventDeliveryConfig(
            events={"comment": "heartbeat", "mention": "heartbeat"}
        )
        self.assertEqual(cfg.mode_for("comment"), "heartbeat")
        self.assertEqual(cfg.mode_for("mention"), "heartbeat")
        self.assertEqual(cfg.mode_for("new-message"), "realtime")

    def test_rejects_deferring_control_events(self):
        with self.assertRaises(ValueError):
            EventDeliveryConfig(events={"interrupt": "heartbeat"})

    def test_rejects_unknown_event_types(self):
        with self.assertRaises(ValueError):
            EventDeliveryConfig(events={"not-a-real-event": "heartbeat"})

    def test_loads_from_agent_config(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        data = {
            "agent": {
                "name": "hermes",
                "model": "openai/gpt-4.1-mini",
                "workspace": "./workspace",
            },
            "modes": {"heartbeat": {"model": "openai/gpt-4.1-mini"}},
            "mcp_servers": [],
            "memory": {
                "extraction_model": "openai/gpt-4.1-mini",
                "embedder": "openai/text-embedding-3-small",
            },
            "event_delivery": {
                "events": {"comment": "heartbeat", "mention": "heartbeat"},
                "notification_inbox": {
                    "expire_after_hours": 48,
                    "max_threads": 10,
                    "categories": ["mentions", "comments"],
                },
            },
        }
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(data))
            config = OuroAgentsConfig.load_from_file(path)

        self.assertEqual(config.event_delivery.mode_for("comment"), "heartbeat")
        self.assertEqual(config.event_delivery.notification_inbox.expire_after_hours, 48)
        self.assertEqual(config.event_delivery.notification_inbox.max_threads, 10)
        self.assertEqual(
            config.event_delivery.notification_inbox.categories,
            ["mentions", "comments"],
        )


class TestNotificationInbox(unittest.TestCase):
    def test_thread_key_prefers_parent_asset(self):
        n = _notif(nid="n1", parent_asset_id="parent-1", asset_id="asset-1")
        self.assertEqual(thread_key_for(n), "parent-1")

    def test_group_threads_merges_same_thread(self):
        now = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
        older = _notif(
            nid="n1",
            text="first",
            created_at=now - timedelta(hours=3),
            username="apollo",
            actor_type="agent",
        )
        newer = _notif(
            nid="n2",
            text="second reply with more detail",
            created_at=now - timedelta(hours=1),
            username="mmoderwell",
            actor_type="human",
        )
        other = _notif(
            nid="n3",
            text="elsewhere",
            parent_asset_id="thread-2",
            asset_id="post-2",
            asset_name="Other post",
            created_at=now - timedelta(hours=2),
            username="will",
            actor_type="human",
        )

        threads = group_threads([older, newer, other], snippet_chars=150)
        self.assertEqual(len(threads), 2)
        # Oldest-waiting thread first
        self.assertEqual(threads[0].thread_key, "thread-1")
        self.assertEqual(threads[0].count, 2)
        self.assertEqual(threads[0].notification_ids, [_uid("n1"), _uid("n2")])
        self.assertEqual(threads[0].latest_actor, "@mmoderwell")
        self.assertIn("second reply", threads[0].latest_snippet)
        self.assertEqual(threads[1].thread_key, "thread-2")

    def test_agent_actor_flagged(self):
        n = _notif(nid="n1", username="apollo", actor_type="agent")
        threads = group_threads([n], snippet_chars=150)
        self.assertEqual(threads[0].latest_actor, "@apollo (agent)")

    def test_snippet_truncation(self):
        long_text = "x" * 200
        n = _notif(nid="n1", text=long_text)
        threads = group_threads([n], snippet_chars=50)
        self.assertLessEqual(len(threads[0].latest_snippet), 50)
        self.assertTrue(threads[0].latest_snippet.endswith("..."))

    def test_render_caps_and_overflow(self):
        now = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
        threads = [
            InboxThread(
                thread_key=f"t{i}",
                asset_name=f"Post {i}",
                asset_type="post",
                notification_ids=[f"n{i}"],
                count=1,
                latest_actor="@alice",
                latest_snippet="hi",
                latest_type="comment",
                oldest_at=now - timedelta(hours=10 - i),
                newest_at=now - timedelta(hours=10 - i),
            )
            for i in range(5)
        ]
        section = render_inbox(
            threads, expired_count=2, max_threads=3, expire_after_hours=72, now=now
        )
        assert section is not None
        self.assertIn("5 thread(s)", section)
        self.assertIn('Post 0"', section)
        self.assertIn('Post 2"', section)
        self.assertNotIn('Post 4"', section)
        self.assertIn("+2 more threads not shown", section)
        self.assertIn("Expired 2 stale notification(s)", section)
        self.assertIn("read_notification(ids=[...])", section)

    def test_fetch_unread_passes_category(self):
        ouro = MagicMock()
        ouro.notifications.list.return_value = Page[Notification](
            data=[_notif(nid="n1"), _notif(nid="n2", viewed=True)]
        )
        items = fetch_unread(
            ouro, max_fetch=50, categories=["mentions", "comments", "shares"]
        )
        ouro.notifications.list.assert_called_once_with(
            unread_only=True,
            limit=50,
            category="mentions,comments,shares",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(str(items[0].id), _uid("n1"))

    def test_expire_stale_marks_old_and_keeps_fresh(self):
        now = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
        stale = _notif(
            nid="old",
            created_at=now - timedelta(hours=100),
        )
        fresh = _notif(
            nid="new",
            created_at=now - timedelta(hours=1),
        )
        ouro = MagicMock()
        remaining, expired = expire_stale(
            ouro, [stale, fresh], expire_after_hours=72, now=now
        )
        self.assertEqual(expired, 1)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(str(remaining[0].id), _uid("new"))
        ouro.notifications.read.assert_called_once_with(_uid("old"))

    def test_build_notification_inbox_happy_path(self):
        now = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
        ouro = MagicMock()
        ouro.notifications.list.return_value = Page[Notification](
            data=[
                _notif(
                    nid="n1",
                    text="@hermes please look",
                    created_at=now - timedelta(hours=2),
                    username="mmoderwell",
                    actor_type="human",
                    notif_type="mention",
                )
            ]
        )
        cfg = NotificationInboxConfig()
        inbox = build_notification_inbox(ouro, cfg, now=now)
        self.assertIsNotNone(inbox.section)
        assert inbox.section is not None
        self.assertIn("Notification Inbox", inbox.section)
        self.assertIn(_uid("n1"), inbox.section)
        self.assertEqual(inbox.thread_count, 1)
        self.assertEqual(inbox.notification_ids, [_uid("n1")])

    def test_build_notification_inbox_swallows_errors(self):
        ouro = MagicMock()
        ouro.notifications.list.side_effect = RuntimeError("boom")
        inbox = build_notification_inbox(ouro, NotificationInboxConfig())
        self.assertIsNone(inbox.section)
        self.assertEqual(inbox.notification_ids, [])
        self.assertEqual(inbox.thread_count, 0)


def _comment_notif(
    *,
    nid: str,
    comment_id: str,
    parent_id: str,
    parent_type: str = "quest",
    created_at: datetime,
    notif_type: str = "mention",
    username: str = "catastropiyush",
) -> Notification:
    """A comment-raised notification shaped the way the backend writes it."""
    return Notification.model_validate(
        {
            "id": _uid(nid),
            "type": notif_type,
            "viewed": False,
            "created_at": created_at,
            "asset_id": comment_id,
            "parent_asset_id": parent_id,
            "content": {
                "text": "mentioned you",
                "asset": {"assetType": "comment", "assetId": comment_id},
                "parent": {"assetType": parent_type, "assetId": parent_id},
            },
            "source_user": {
                "user_id": _uid(username),
                "username": username,
                "actor_type": "human",
            },
        }
    )


def _comment(*, cid: str, username: str, text: str, created_at: datetime):
    comment = MagicMock()
    comment.id = cid
    comment.user_id = _uid(username)
    comment.user.username = username
    comment.text = text
    comment.created_at = created_at
    return comment


class TestInboxThreadContext(unittest.TestCase):
    NOW = datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)
    ASK = _uid("ask-comment")
    QUEST = _uid("quest")

    def _ouro(self, replies):
        ouro = MagicMock()
        ouro.notifications.list.return_value = Page[Notification](
            data=[
                _comment_notif(
                    nid="n1",
                    comment_id=self.ASK,
                    parent_id=self.QUEST,
                    created_at=self.NOW - timedelta(hours=2),
                )
            ]
        )
        ouro.comments.retrieve.return_value = _comment(
            cid=self.ASK,
            username="catastropiyush",
            text="@hermes any new updates here?",
            created_at=self.NOW - timedelta(hours=2),
        )
        ouro.comments.list.return_value = replies
        return ouro

    def test_own_later_reply_is_flagged_as_already_answered(self):
        ouro = self._ouro(
            [
                _comment(
                    cid=_uid("first-reply"),
                    username="hermes",
                    text="Nothing new since Sep 23; compute is paused.",
                    created_at=self.NOW - timedelta(hours=1),
                )
            ]
        )
        inbox = build_notification_inbox(
            ouro, NotificationInboxConfig(), now=self.NOW, own_user_id=_uid("hermes")
        )
        assert inbox.section is not None
        ouro.comments.list.assert_called_once_with(self.ASK, limit=50)
        self.assertIn("@hermes any new updates here?", inbox.section)
        self.assertIn("[notified you]", inbox.section)
        self.assertIn("@hermes (you), 1h ago", inbox.section)
        self.assertIn("ALREADY ANSWERED: you commented", inbox.section)

    def test_unanswered_thread_is_not_flagged(self):
        ouro = self._ouro(
            [
                _comment(
                    cid=_uid("other-reply"),
                    username="apollo",
                    text="Curious too.",
                    created_at=self.NOW - timedelta(hours=1),
                )
            ]
        )
        inbox = build_notification_inbox(
            ouro, NotificationInboxConfig(), now=self.NOW, own_user_id=_uid("hermes")
        )
        assert inbox.section is not None
        self.assertIn("Curious too.", inbox.section)
        self.assertNotIn("ALREADY ANSWERED: you commented", inbox.section)
        self.assertNotIn("(you)", inbox.section)

    def test_own_reply_older_than_notification_is_not_an_answer(self):
        ouro = self._ouro(
            [
                _comment(
                    cid=_uid("old-reply"),
                    username="hermes",
                    text="Earlier note.",
                    created_at=self.NOW - timedelta(hours=5),
                )
            ]
        )
        inbox = build_notification_inbox(
            ouro, NotificationInboxConfig(), now=self.NOW, own_user_id=_uid("hermes")
        )
        assert inbox.section is not None
        self.assertIn("(you)", inbox.section)
        self.assertNotIn("ALREADY ANSWERED: you commented", inbox.section)

    def test_reply_notification_loads_the_parent_comment_thread(self):
        thread_head = _uid("thread-head")
        ouro = self._ouro([])
        ouro.notifications.list.return_value = Page[Notification](
            data=[
                _comment_notif(
                    nid="n2",
                    comment_id=_uid("sibling-reply"),
                    parent_id=thread_head,
                    parent_type="comment",
                    created_at=self.NOW - timedelta(hours=1),
                    notif_type="reply",
                )
            ]
        )
        build_notification_inbox(ouro, NotificationInboxConfig(), now=self.NOW)
        ouro.comments.list.assert_called_once_with(thread_head, limit=50)

    def test_context_failure_keeps_the_notification(self):
        ouro = self._ouro([])
        ouro.comments.list.side_effect = RuntimeError("boom")
        inbox = build_notification_inbox(ouro, NotificationInboxConfig(), now=self.NOW)
        assert inbox.section is not None
        self.assertIn(_uid("n1"), inbox.section)
        self.assertNotIn("Thread on comment", inbox.section)

    def test_thread_context_can_be_disabled(self):
        ouro = self._ouro([])
        build_notification_inbox(
            ouro, NotificationInboxConfig(thread_context=False), now=self.NOW
        )
        ouro.comments.list.assert_not_called()


class TestMarkReadOnReply(unittest.TestCase):
    NOW = datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)
    ASK = _uid("ask-comment")
    QUEST = _uid("quest")
    THREAD = _uid("thread-head")

    def _ouro(self):
        ouro = MagicMock()
        ouro.notifications.list.return_value = Page[Notification](
            data=[
                _comment_notif(
                    nid="direct",
                    comment_id=self.ASK,
                    parent_id=self.QUEST,
                    created_at=self.NOW,
                ),
                _comment_notif(
                    nid="in-thread",
                    comment_id=_uid("sibling-reply"),
                    parent_id=self.THREAD,
                    parent_type="comment",
                    created_at=self.NOW,
                    notif_type="reply",
                ),
                _comment_notif(
                    nid="unrelated",
                    comment_id=_uid("other-comment"),
                    parent_id=self.QUEST,
                    created_at=self.NOW,
                ),
            ]
        )
        return ouro

    def _wrapped(self, ouro, result):
        tool = MagicMock()
        tool.forward = MagicMock(return_value=result)
        wrap_write_comment_with_mark_read(
            tool, get_ouro=lambda: ouro, get_config=NotificationInboxConfig
        )
        return tool

    def test_reply_to_notifying_comment_marks_it_read(self):
        ouro = self._ouro()
        marked = mark_replied_notifications_read(
            ouro, [self.ASK], NotificationInboxConfig()
        )
        self.assertEqual(marked, [_uid("direct")])
        ouro.notifications.read.assert_called_once_with(_uid("direct"))

    def test_reply_in_comment_thread_marks_sibling_notifications_read(self):
        ouro = self._ouro()
        marked = mark_replied_notifications_read(
            ouro, [self.THREAD], NotificationInboxConfig()
        )
        self.assertEqual(marked, [_uid("in-thread")])

    def test_top_level_comment_on_root_asset_clears_nothing(self):
        ouro = self._ouro()
        marked = mark_replied_notifications_read(
            ouro, [self.QUEST], NotificationInboxConfig()
        )
        self.assertEqual(marked, [])
        ouro.notifications.read.assert_not_called()

    def test_wrapped_tool_marks_read_after_posting(self):
        ouro = self._ouro()
        result = json.dumps({"id": _uid("new-reply"), "parent_id": self.ASK})
        tool = self._wrapped(ouro, result)
        self.assertEqual(tool.forward(parent_id=self.ASK, content_markdown="hi"), result)
        ouro.notifications.read.assert_called_once_with(_uid("direct"))

    def test_wrapped_tool_follows_backend_reparenting(self):
        ouro = self._ouro()
        result = json.dumps({"id": _uid("new-reply"), "parent_id": self.THREAD})
        tool = self._wrapped(ouro, result)
        tool.forward({"parent_id": _uid("sibling-reply"), "content_markdown": "hi"})
        ouro.notifications.read.assert_called_once_with(_uid("in-thread"))

    def test_failed_comment_marks_nothing(self):
        ouro = self._ouro()
        tool = self._wrapped(ouro, "Error: attribution is required")
        tool.forward(parent_id=self.ASK, content_markdown="hi")
        ouro.notifications.list.assert_not_called()
        ouro.notifications.read.assert_not_called()

    def test_mark_read_failure_never_breaks_the_comment(self):
        ouro = self._ouro()
        ouro.notifications.list.side_effect = RuntimeError("boom")
        result = json.dumps({"id": _uid("new-reply"), "parent_id": self.ASK})
        tool = self._wrapped(ouro, result)
        self.assertEqual(tool.forward(parent_id=self.ASK), result)


class TestHeartbeatDeliveryGate(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_mode_returns_without_running(self):
        from unittest.mock import AsyncMock, patch

        from ouro_agents import server
        from ouro_agents.config import EventDeliveryConfig, RunMode
        from ouro_agents.events import EventRunContext

        event_run = EventRunContext(
            event_type="comment",
            task="comment task",
            mode=RunMode.AUTONOMOUS,
            conversation_id=None,
            user_id="user-other",
            actor_user_id="user-other",
            actor_username="apollo",
            source_id="comment-1",
            notification_ids=("notif-1",),
        )

        fake_agent = MagicMock()
        fake_agent.own_user_id = "user-hermes"
        fake_agent.config.event_delivery = EventDeliveryConfig(
            events={"comment": "heartbeat", "mention": "heartbeat"}
        )
        fake_agent.config.security.resolved_controller_ids = ["user-controller"]

        with (
            patch.object(server, "agent_instance", fake_agent),
            patch.object(
                server, "build_event_run_context", return_value=event_run
            ),
            patch.object(server, "resolve_event_provenance", return_value=None),
            patch.object(
                server, "_mark_event_notifications_read", new_callable=AsyncMock
            ) as mark_read,
            patch.object(server, "event_pool", None),
        ):
            background = MagicMock()
            background.add_task = MagicMock()
            result = await server.process_event(
                {"event": "comment", "data": {}}, background
            )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["delivery"], "heartbeat")
        mark_read.assert_not_called()
        background.add_task.assert_not_called()

    async def test_controller_bypasses_heartbeat_delivery(self):
        from unittest.mock import patch

        from ouro_agents import server
        from ouro_agents.config import EventDeliveryConfig, RunMode
        from ouro_agents.events import EventRunContext

        event_run = EventRunContext(
            event_type="mention",
            task="mention task",
            mode=RunMode.AUTONOMOUS,
            conversation_id=None,
            user_id="user-controller",
            actor_user_id="user-controller",
            actor_username="mmoderwell",
            source_id="comment-2",
            notification_ids=("notif-2",),
        )

        fake_agent = MagicMock()
        fake_agent.own_user_id = "user-hermes"
        fake_agent.config.event_delivery = EventDeliveryConfig(
            events={"comment": "heartbeat", "mention": "heartbeat"}
        )
        fake_agent.config.security.resolved_controller_ids = ["user-controller"]

        with (
            patch.object(server, "agent_instance", fake_agent),
            patch.object(
                server, "build_event_run_context", return_value=event_run
            ),
            patch.object(server, "resolve_event_provenance", return_value=None),
            patch.object(server, "event_pool", None),
        ):
            background = MagicMock()
            background.add_task = MagicMock()
            result = await server.process_event(
                {"event": "mention", "data": {}}, background
            )

        self.assertEqual(result["status"], "accepted")
        self.assertNotEqual(result.get("delivery"), "heartbeat")
        self.assertEqual(result.get("pooled"), False)
        background.add_task.assert_called_once()

    async def test_controller_bypass_can_be_disabled(self):
        from unittest.mock import AsyncMock, patch

        from ouro_agents import server
        from ouro_agents.config import EventDeliveryConfig, RunMode
        from ouro_agents.events import EventRunContext

        event_run = EventRunContext(
            event_type="comment",
            task="comment task",
            mode=RunMode.AUTONOMOUS,
            conversation_id=None,
            user_id="user-controller",
            actor_user_id="user-controller",
            actor_username="mmoderwell",
            source_id="comment-3",
            notification_ids=("notif-3",),
        )

        fake_agent = MagicMock()
        fake_agent.own_user_id = "user-hermes"
        fake_agent.config.event_delivery = EventDeliveryConfig(
            events={"comment": "heartbeat"},
            realtime_for_controllers=False,
        )
        fake_agent.config.security.resolved_controller_ids = ["user-controller"]

        with (
            patch.object(server, "agent_instance", fake_agent),
            patch.object(
                server, "build_event_run_context", return_value=event_run
            ),
            patch.object(server, "resolve_event_provenance", return_value=None),
            patch.object(
                server, "_mark_event_notifications_read", new_callable=AsyncMock
            ) as mark_read,
            patch.object(server, "event_pool", None),
        ):
            background = MagicMock()
            background.add_task = MagicMock()
            result = await server.process_event(
                {"event": "comment", "data": {}}, background
            )

        self.assertEqual(result["delivery"], "heartbeat")
        mark_read.assert_not_called()
        background.add_task.assert_not_called()

    async def test_realtime_mode_still_dispatches(self):
        from unittest.mock import patch

        from ouro_agents import server
        from ouro_agents.config import EventDeliveryConfig, RunMode
        from ouro_agents.events import EventRunContext

        event_run = EventRunContext(
            event_type="comment",
            task="comment task",
            mode=RunMode.AUTONOMOUS,
            conversation_id=None,
            user_id="user-other",
            actor_user_id="user-other",
            actor_username="alice",
            source_id="comment-1",
            notification_ids=("notif-1",),
        )

        fake_agent = MagicMock()
        fake_agent.own_user_id = "user-hermes"
        fake_agent.config.event_delivery = EventDeliveryConfig()  # all realtime
        fake_agent.config.security.resolved_controller_ids = []

        with (
            patch.object(server, "agent_instance", fake_agent),
            patch.object(
                server, "build_event_run_context", return_value=event_run
            ),
            patch.object(server, "resolve_event_provenance", return_value=None),
            patch.object(server, "event_pool", None),
        ):
            background = MagicMock()
            background.add_task = MagicMock()
            result = await server.process_event(
                {"event": "comment", "data": {}}, background
            )

        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result.get("delivery") == "heartbeat")
        self.assertEqual(result.get("pooled"), False)
        background.add_task.assert_called_once()


class TestShouldDeferToHeartbeat(unittest.TestCase):
    def test_defaults(self):
        cfg = EventDeliveryConfig(events={"comment": "heartbeat"})
        self.assertTrue(
            cfg.should_defer_to_heartbeat("comment", actor_user_id="u1")
        )
        self.assertFalse(
            cfg.should_defer_to_heartbeat(
                "comment",
                actor_user_id="ctrl",
                controller_user_ids=["ctrl"],
            )
        )
        self.assertFalse(cfg.should_defer_to_heartbeat("new-message"))


if __name__ == "__main__":
    unittest.main()
