# Events & webhooks

The FastAPI server (`ouro_agents/server.py`) accepts Ouro webhook events
at `server.webhook_path` (default `/events`) and routes them through a
small registry that decides:

- whether the event is a **chat event** (drives realtime activity / streaming),
- which MCP tools to **preload** for the run,
- whether to **pool** bursts of events into a single dispatch,
- whether delivery is **realtime** or deferred to the **heartbeat** inbox,
- and which special path to take (cleanup, plan-feedback, normal run).

## Delivery modes

`event_delivery` controls whether a webhook runs the agent immediately or
is deferred until the next heartbeat:

| Mode | Behavior |
|------|----------|
| `realtime` (default) | Acknowledge, optionally pool, then run the agent. Notifications are marked read on dispatch. |
| `heartbeat` | Acknowledge and return. Notifications stay **unread** and surface in the next heartbeat's Notification Inbox. |

Controllers bypass heartbeat deferral by default (`realtime_for_controllers: true`):
their comments/mentions still wake the agent immediately. Trusted users and other
agents do not — that keeps agent↔agent chatter on the heartbeat cadence.

```json
"event_delivery": {
  "events": {
    "comment": "heartbeat",
    "mention": "heartbeat"
  },
  "realtime_for_controllers": true,
  "notification_inbox": {
    "expire_after_hours": 72,
    "max_threads": 15,
    "categories": ["mentions", "comments", "shares"]
  }
}
```

Control / cleanup events (`interrupt`, `asset.deleted`, `new-conversation`)
must stay realtime — the config validator rejects deferring them.

### Notification Inbox triage

When the heartbeat builds its playbook it fetches unread notifications in
the configured categories, auto-expires anything older than
`expire_after_hours`, groups the rest by thread, and appends a compact
`## Notification Inbox` section. Per thread the agent chooses:

- **Handle** — reply (e.g. `write_comment`), then include the thread's ids
  in one batch `read_notification(ids=[...])` call.
- **Dismiss** — nothing worth doing; include ids in the same batch call
  without acting.
- **Defer** — do nothing; ids stay unread and reappear next tick.

Unread on the platform **is** the queue. There is no local ledger. Silence
is the default; planned / quest work stays primary.

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

Payload-dependent extras are composed in `ouro_agents/tool_preloads.py`
(`preloads_for_event`): quest comments also preload quest-manage tools, and
a chat message with an attached asset preloads `ouro:get_asset`. Role and
surface never add tools — they only subtract, via the capability envelope.

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
| `notification_ids` | Marked as read after realtime dispatch (not for heartbeat delivery). |
| `prefetch` | Pre-resolved asset blobs to inject into the prompt. |
| `preload_tools` | Tools to preload (overrides registry preloads). |
| `provenance` | Plan-feedback metadata; see below. |
| `thread_parent_id`, `reply_parent_id`, `root_asset_id`, `source_id` | Used for pool-key computation. |

## Pool keys and debouncing

Each event spec carries a `pool_key_fn`. Two strategies ship today:

- `_conversation_pool_key` — keys on conversation id (available if chat
  pooling is re-enabled; off by default for reply latency).
- `_thread_pool_key` — keys on the thread (or root asset) id so a flurry
  of comments on the same post is handled once.

The `EventPool` (`ouro_agents/event_pool.py`) is a debounce buffer:

- `settle_seconds` — wait at least this long after the last event in a
  batch before dispatching.
- `jitter_seconds` — random extra wait per batch (smooths thundering
  herds across multiple agents).
- `max_wait_seconds` — hard ceiling per batch from the first event.

Defaults are tuned per event type in `EventPoolingConfig` and merged
with user overrides field-by-field. Pooling only applies to events whose
delivery mode is `realtime`.

## Routing inside `handle_event` / `_run_event_task`

The order of branches in the server handler:

1. **Self-event guard** — drop events whose actor is the agent's own
   user id.
2. **Interrupt** — cancel in-flight chat work; no LLM run.
3. **Heartbeat delivery** — if the event should defer to the heartbeat
   inbox (delivery mode `heartbeat`, and the actor is not a controller
   when `realtime_for_controllers` is on), return accepted without marking
   notifications read and without running.
4. **Controller decision** — short-circuit for live ask-controller replies.
5. **Pooling** — coalesce poolable realtime events, then dispatch.
6. Inside `_run_event_task`:
   - **Cleanup** — `asset.deleted` runs deterministic cleanup; no LLM.
   - **Mark notifications read** — best-effort, before the realtime run.
   - **`new-conversation`** — no-op until a message arrives.
   - **Normal run** — otherwise call `OuroAgent.run(...)`.

## Provenance

`resolve_event_provenance` inspects the incoming payload (thread-root
asset and author) against the cached agent identity and produces an
`AssetProvenance` with:

- `is_own_asset`
- `root_asset_id` / `root_asset_type`
- `team_id`

## Adding a new event type

1. Make sure the event name is in
   `ouro.events.WEBHOOK_EVENT_TYPES` (canonical registry mirrored from
   `ouro-js`).
2. Add an `EventSpec` to `EVENT_REGISTRY` with appropriate `is_chat`,
   `tool_preloads`, and `pool_key_fn`.
3. Optionally set `event_delivery.events[<name>]` to `heartbeat` if the
   event should wait for the next tick instead of running immediately.
4. If the event needs a special path (deterministic cleanup, planning
   side effects, etc.), branch on `event_type` in `_run_event_task`
   before falling through to the generic run.
