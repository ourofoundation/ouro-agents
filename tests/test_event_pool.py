import asyncio
import unittest

from ouro_agents.config import EventPoolingConfig, EventPoolTimingConfig, RunMode
from ouro_agents.event_pool import EventPool, build_pooled_event_run
from ouro_agents.events import EventRunContext


def _timing(settle: float = 0.01) -> EventPoolTimingConfig:
    return EventPoolTimingConfig(
        settle_seconds=settle,
        jitter_seconds=0,
        max_wait_seconds=0.1,
    )


def _config(**events: EventPoolTimingConfig) -> EventPoolingConfig:
    return EventPoolingConfig(enabled=True, events=dict(events))


def _event(
    event_type: str = "new-message",
    *,
    conversation_id: str | None = "conv-1",
    root_asset_id: str | None = None,
    thread_parent_id: str | None = None,
    source_id: str | None = None,
    text: str = "hello",
    username: str = "alice",
    is_agent: bool = False,
    notification_ids: tuple[str, ...] = (),
) -> EventRunContext:
    return EventRunContext(
        event_type=event_type,
        task=f"Task for {text}",
        mode=RunMode.CHAT_REPLY if event_type == "new-message" else RunMode.AUTONOMOUS,
        conversation_id=conversation_id,
        user_id=f"user-{username}",
        source_id=source_id,
        root_asset_id=root_asset_id,
        root_asset_type="post" if root_asset_id else None,
        reply_parent_id=source_id if event_type in {"comment", "mention"} else None,
        thread_parent_id=thread_parent_id,
        feedback_text=text if event_type in {"comment", "mention"} else None,
        actor_user_id=f"user-{username}",
        actor_username=username,
        actor_is_agent=is_agent,
        event_text=text,
        received_at="2026-04-30T22:00:00Z",
        notification_ids=notification_ids,
    )


class TestEventPool(unittest.TestCase):
    def test_pool_key_and_non_poolable_events(self):
        async def dispatch(event_run: EventRunContext) -> None:
            dispatched.append(event_run)

        dispatched: list[EventRunContext] = []
        pool = EventPool(_config(**{"new-message": _timing()}), dispatch)

        self.assertEqual(
            pool.pool_key(_event("new-message", conversation_id="conv-1")),
            "conversation:conv-1",
        )
        self.assertEqual(
            pool.pool_key(
                _event(
                    "comment",
                    conversation_id=None,
                    root_asset_id="post-1",
                    thread_parent_id="thread-1",
                )
            ),
            "thread:thread-1",
        )
        self.assertFalse(pool.is_poolable(_event("unknown-event")))

    def test_comment_and_mention_payload_variants_share_thread_key(self):
        async def dispatch(event_run: EventRunContext) -> None:
            dispatched.append(event_run)

        dispatched: list[EventRunContext] = []
        pool = EventPool(
            _config(**{"comment": _timing(), "mention": _timing()}),
            dispatch,
        )

        comment_event = _event(
            "comment",
            conversation_id=None,
            root_asset_id="post-1",
            thread_parent_id="thread-1",
            source_id="comment-1",
        )
        mention_event = _event(
            "mention",
            conversation_id=None,
            root_asset_id="mentioned-user",
            thread_parent_id="thread-1",
            source_id="comment-1",
        )

        self.assertEqual(pool.pool_key(comment_event), pool.pool_key(mention_event))
        self.assertEqual(pool.pool_key(comment_event), "thread:thread-1")

    def test_top_level_comments_pool_by_source_comment(self):
        async def dispatch(event_run: EventRunContext) -> None:
            dispatched.append(event_run)

        dispatched: list[EventRunContext] = []
        pool = EventPool(
            _config(**{"comment": _timing(), "mention": _timing()}),
            dispatch,
        )

        first_comment = _event(
            "comment",
            conversation_id=None,
            root_asset_id="post-1",
            thread_parent_id="post-1",
            source_id="comment-1",
            text="first top-level",
        )
        second_comment = _event(
            "comment",
            conversation_id=None,
            root_asset_id="post-1",
            thread_parent_id="post-1",
            source_id="comment-2",
            text="second top-level",
        )
        mention_for_first = _event(
            "mention",
            conversation_id=None,
            root_asset_id="post-1",
            thread_parent_id="post-1",
            source_id="comment-1",
            text="@agent first top-level",
        )

        self.assertEqual(pool.pool_key(first_comment), "thread:comment-1")
        self.assertEqual(pool.pool_key(second_comment), "thread:comment-2")
        self.assertEqual(pool.pool_key(mention_for_first), "thread:comment-1")

    def test_multiple_events_for_one_key_dispatch_once(self):
        async def exercise() -> list[EventRunContext]:
            dispatched: list[EventRunContext] = []

            async def dispatch(event_run: EventRunContext) -> None:
                dispatched.append(event_run)

            pool = EventPool(_config(**{"new-message": _timing()}), dispatch)
            await pool.submit(_event(text="first"))
            await asyncio.sleep(0.005)
            await pool.submit(_event(text="second"))
            await asyncio.sleep(0.04)
            await pool.stop()
            return dispatched

        dispatched = asyncio.run(exercise())

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].event_text, "second")
        self.assertIn("Pooled Event Batch", dispatched[0].task)
        self.assertIn("first", dispatched[0].task)
        self.assertIn("second", dispatched[0].task)
        self.assertIn("Reply at most once", dispatched[0].task)

    def test_single_event_batch_has_no_pooled_context(self):
        event = _event(text="solo", notification_ids=("n-1",))
        pooled = build_pooled_event_run([event])
        self.assertEqual(pooled.task, event.task)
        self.assertNotIn("Pooled Event Batch", pooled.task)
        self.assertEqual(pooled.notification_ids, ("n-1",))

    def test_different_keys_dispatch_separately(self):
        async def exercise() -> list[EventRunContext]:
            dispatched: list[EventRunContext] = []

            async def dispatch(event_run: EventRunContext) -> None:
                dispatched.append(event_run)

            pool = EventPool(_config(**{"new-message": _timing()}), dispatch)
            await pool.submit(_event(conversation_id="conv-1", text="one"))
            await pool.submit(_event(conversation_id="conv-2", text="two"))
            await asyncio.sleep(0.03)
            await pool.stop()
            return dispatched

        dispatched = asyncio.run(exercise())

        self.assertEqual(len(dispatched), 2)
        self.assertEqual({event.event_text for event in dispatched}, {"one", "two"})

    def test_same_key_waits_for_active_dispatch(self):
        async def exercise() -> list[str]:
            dispatched: list[str] = []
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            async def dispatch(event_run: EventRunContext) -> None:
                dispatched.append(event_run.event_text or "")
                if event_run.event_text == "first":
                    first_started.set()
                    await release_first.wait()

            pool = EventPool(_config(**{"new-message": _timing()}), dispatch)
            await pool.submit(_event(text="first"))
            await first_started.wait()

            await pool.submit(_event(text="second"))
            await asyncio.sleep(0.04)
            self.assertEqual(dispatched, ["first"])

            release_first.set()
            await asyncio.sleep(0.04)
            await pool.stop()
            return dispatched

        dispatched = asyncio.run(exercise())

        self.assertEqual(dispatched, ["first", "second"])

    def test_event_type_specific_windows_control_dispatch_order(self):
        async def exercise() -> list[str]:
            dispatched: list[str] = []

            async def dispatch(event_run: EventRunContext) -> None:
                dispatched.append(event_run.event_type)

            pool = EventPool(
                _config(
                    **{
                        "new-message": _timing(0.005),
                        "comment": _timing(0.03),
                    }
                ),
                dispatch,
            )
            await pool.submit(_event("new-message", text="chat"))
            await pool.submit(
                _event(
                    "comment",
                    conversation_id=None,
                    root_asset_id="post-1",
                    thread_parent_id="post-1",
                    source_id="comment-1",
                    text="comment",
                )
            )
            await asyncio.sleep(0.06)
            await pool.stop()
            return dispatched

        dispatched = asyncio.run(exercise())

        self.assertEqual(dispatched, ["new-message", "comment"])

    def test_pooled_context_updates_feedback_text_for_plan_feedback(self):
        pooled = build_pooled_event_run(
            [
                _event(
                    "comment",
                    conversation_id=None,
                    root_asset_id="post-1",
                    source_id="comment-1",
                    text="first feedback",
                    is_agent=True,
                ),
                _event(
                    "comment",
                    conversation_id=None,
                    root_asset_id="post-1",
                    source_id="comment-2",
                    text="second feedback",
                    username="bot",
                    is_agent=True,
                ),
            ]
        )

        self.assertIn("second feedback", pooled.feedback_text)
        self.assertIn("first feedback", pooled.feedback_text)
        self.assertIn("@bot (agent)", pooled.feedback_text)
        self.assertNotIn("All events in this batch came from agents", pooled.feedback_text)

    def test_pooled_event_merges_notification_ids(self):
        pooled = build_pooled_event_run(
            [
                _event("comment", notification_ids=("notification-1",)),
                _event(
                    "mention",
                    notification_ids=("notification-2", "notification-1"),
                ),
            ]
        )

        self.assertEqual(
            pooled.notification_ids,
            ("notification-1", "notification-2"),
        )


if __name__ == "__main__":
    unittest.main()
