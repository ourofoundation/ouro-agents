# Events & webhooks

The FastAPI server (`ouro_agents/server.py`) accepts Ouro webhook events
at `server.webhook_path` (default `/events`) and routes them through a
small registry that decides:

- whether the event is a **chat event** (drives realtime activity / streaming),
- which MCP tools to **preload** for the run,
- whether to **pool** bursts of events into a single dispatch,
- and which special path to take (cleanup, plan-feedback, normal run).

## Event registry

`ouro_agents/event_registry.py` maps each event name to an `EventSpec`:

```python
EVENT_REGISTRY = {
  "comment":          EventSpec(tool_preloads=COMMENT_PRELOADS, pool_key_fn=_thread_pool_key),
  "mention":          EventSpec(tool_preloads=COMMENT_PRELOADS, pool_key_fn=_thread_pool_key),
  "new-message":      EventSpec(is_chat=True, pool_key_fn=_conversation_pool_key),
  "new-conversation": EventSpec(is_chat=True),
  "asset.deleted":    EventSpec(),  # handled by cleanup, no preloads, no pooling
}
```

Comment / mention preloads: `ouro:get_asset`, `ouro:write_comment`,
`ouro:get_comments`. New events get a zero-config `EventSpec` by default.

The registry validates against `ouro.events.WEBHOOK_EVENT_TYPES` at
import time, so a typo on a key fails fast.

## EventRunContext

Every webhook is parsed into an `EventRunContext`
(`ouro_agents/events.py`):

| Field | Notes |
|-------|-------|
| `event_type` | Webhook name (e.g. `comment`, `new-message`). |
| `task` | Constructed task string for the agent. |
| `mode` | The `RunMode` to dispatch with. |
| `user_id` | Actor's user id (or `None`). |
| `actor_user_id` | Used for the self-event guard. |
| `conversation_id` | When the event has one. |
| `team_id` | Team scope, if any. |
| `notification_ids` | Marked as read after dispatch. |
| `prefetch` | Pre-resolved asset blobs to inject into the prompt. |
| `preload_tools` | Tools to preload (overrides registry preloads). |
| `provenance` | Plan-feedback metadata; see below. |
| `thread_parent_id`, `reply_parent_id`, `root_asset_id`, `source_id` | Used for pool-key computation. |

## Pool keys and debouncing

Each event spec carries a `pool_key_fn`. Two strategies ship today:

- `_conversation_pool_key` — keys on conversation id; bursts of chat
  messages collapse into one run per conversation.
- `_thread_pool_key` — keys on the thread (or root asset) id so a flurry
  of comments on the same post is handled once.

The `EventPool` (`ouro_agents/event_pool.py`) is a debounce buffer:

- `settle_seconds` — wait at least this long after the last event in a
  batch before dispatching.
- `jitter_seconds` — random extra wait per batch (smooths thundering
  herds across multiple agents).
- `max_wait_seconds` — hard ceiling per batch from the first event.

Defaults are tuned per event type in `EventPoolingConfig` and merged
with user overrides field-by-field.

## Routing inside `_run_event_task`

The order of branches in the server handler:

1. **Self-event guard** — drop events whose actor is the agent's own
   user id.
2. **Cleanup** — `asset.deleted` runs the deterministic cleanup and
   returns. No LLM is invoked.
3. **Mark notifications read** — best-effort, before the run starts.
4. **Quest feedback** — if `provenance.is_quest_feedback`, dispatch the
   review run through `OuroAgent.handle_quest_feedback`.
5. **`new-conversation`** — no-op; nothing to respond to until a message
   arrives.
6. **Normal run** — otherwise, build a `ServerAgentObserver` (which
   streams activity into Ouro for chat events) and call
   `OuroAgent.run(task, mode, conversation_id, team_id, prefetch, ...)`.

## Provenance and quest-feedback events

`resolve_event_provenance` inspects the incoming payload (thread-root
asset and author) against the cached agent identity and produces an
`AssetProvenance` with:

- `is_own_asset`
- `root_asset_id` / `root_asset_type`
- `is_quest_feedback` (own asset whose thread root is a quest)
- `team_id`

When `is_quest_feedback=True`, the handler routes through
`handle_quest_feedback`, which re-verifies quest ownership against the
platform and dispatches a `review` run with the inline feedback text.
If verification fails, the event falls back to the normal comment run.

## Adding a new event type

1. Make sure the event name is in
   `ouro.events.WEBHOOK_EVENT_TYPES` (canonical registry mirrored from
   `ouro-js`).
2. Add an `EventSpec` to `EVENT_REGISTRY` with appropriate `is_chat`,
   `tool_preloads`, and `pool_key_fn`.
3. If the event needs a special path (deterministic cleanup, planning
   side effects, etc.), branch on `event_type` in `_run_event_task`
   before falling through to the generic run.
