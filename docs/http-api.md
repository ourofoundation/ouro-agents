# HTTP API & webhooks

`ouro-agents serve` boots a FastAPI app (`ouro_agents/server.py`) that:

- Owns a single shared `OuroAgent` instance for the lifetime of the
  process.
- Exposes `/run`, `/health`, `/tasks`, and a webhook path
  (`server.webhook_path`, default `/events`).
- Streams replies back to Ouro chat conversations via the realtime API
  when handling chat events.
- Pools bursts of webhook events into single agent runs.

## Lifecycle

The app uses `lifespan(app)` instead of `@app.on_event`. On startup it:

1. Loads `config.json` (path from `CONFIG_FILE`, set by `start_server`).
2. Builds an `OuroAgent`, connects MCP servers, and refreshes platform
   context (orgs/teams).
3. Constructs an `EventPool` and an `OuroReplyPublisher`.
4. Mounts the webhook route at `config.server.webhook_path`.
5. Starts the agent's `AgentScheduler` (heartbeat + consolidation +
   refinement + user tasks).

On shutdown it stops the event pool, shuts the scheduler down, and closes
all MCP server connections.

## `POST /run`

Request body:

```json
{
  "task": "What teams am I on?",
  "conversation_id": null,
  "session_id": "demo-user-1",
  "mode": null,
  "user_id": null
}
```

| Field | Notes |
|-------|-------|
| `task` | Required. The user message. |
| `conversation_id` | Pin to a specific conversation; created with a UUID v7 when omitted. |
| `session_id` | Optional convenience: the server keeps an in-memory map from session id → conversation id, so repeat requests with the same `session_id` join the same thread. |
| `mode` | One of `chat`, `autonomous`, `heartbeat`, `plan`, `review`. Defaults to `autonomous`. The legacy values `chat-reply`/`reply` are accepted and resolve to `chat`. |
| `user_id` | Identity of the human you're proxying for; threaded into reflection and the user-model file. |

Response:

```json
{
  "status": "success",
  "result": "...",
  "conversation_id": "01H..."
}
```

Errors return HTTP 500 with the exception message in `detail`. If the
agent isn't initialized yet (still inside `lifespan` startup), the route
returns 503.

### Threaded conversation example

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Hi, can you help me post a dataset?","session_id":"demo-user-1"}'

curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Use the Machine Learning team","session_id":"demo-user-1"}'
```

The second call reuses the conversation id from the first (lookup keyed
by `session_id`).

## `GET /health`

```json
{
  "status": "ok",
  "uptime_seconds": 12345.6,
  "last_heartbeat": "2026-04-30T14:00:00+00:00",
  "agent_name": "hermes",
  "scheduled_tasks": 3
}
```

## `GET /tasks`

Lists the scheduler's known scheduled tasks (system + user-created). Each
entry is a `ScheduledTask` model dump.

## `POST <webhook_path>` — Ouro events

`server.webhook_path` (default `/events`) accepts platform events. The
handler:

1. Builds an `EventRunContext` from the body via
   `build_event_run_context` and `resolve_event_provenance`.
2. Drops events the agent itself triggered (self-event guard).
3. Marks correlated notifications as read (best-effort).
4. Routes:
   - `asset.deleted` → deterministic cleanup, no LLM. See
     [Cleanup](./cleanup.md).
   - Plan-feedback events → `OuroAgent.handle_plan_feedback` → review
     heartbeat. See [Planning](./planning.md).
   - `new-conversation` → no-op.
   - Otherwise → `OuroAgent.run(...)` in the appropriate mode with a
     `ServerAgentObserver` that streams the reply back to Ouro.

If `event_pool.is_poolable(event)` returns True, the handler defers
dispatch through the pool (debouncing burst events for the same
conversation/comment). Otherwise the run starts immediately as a FastAPI
background task.

Webhook responses always return `{"status": "accepted", ...}`. Actual
processing happens out-of-band; clients should not wait on the response
for run results.

### Chat streaming

For chat-driven events, the `ServerAgentObserver`:

- Emits `typing` and `thinking` activity through the realtime API.
- Streams final-reply chunks back as `llm_response` messages keyed by
  a server-generated `stream_message_id`.
- Persists the final assistant message to the conversation when the run
  completes.

This is what makes the agent feel "live" in an Ouro chat panel.

## Self-event safety

The server guards against reply loops in two places:

1. Before the event pool — if the event's actor matches the agent's own
   user id, it returns `accepted` with `skipped=true`.
2. Inside `_run_event_task`, as a second safety net.

The backend filters most self-triggered events too, but these guards make
the agent process safe even if a webhook leaks through.

## Programmatic startup

```python
from ouro_agents import start_server

start_server("config.json")
```

This wraps `uvicorn.run("ouro_agents.server:app", host=..., port=...)`
with the right log config and reload policy.
