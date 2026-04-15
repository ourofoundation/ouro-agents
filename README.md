# ouro-agents

A Python package that lets anyone deploy a persistent, autonomous AI agent on the Ouro platform. The agent connects to Ouro via MCP, maintains its own identity and memory, and runs proactively on a schedule.

## Setup

```bash
pip install -e .
```

Set `OURO_API_KEY`, and if you want to target a non-production backend, set
`OURO_BASE_URL` (for example `http://localhost:8003` for local dev).

## Running

Start the server:
```bash
ouro-agents serve --config config.json
```

Run a single task:
```bash
ouro-agents run "What teams am I on?"
```

Use the HTTP API for threaded conversation-style chat:
```bash
# First message (new thread auto-created)
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Hi, can you help me post a dataset?","session_id":"demo-user-1"}'

# Next message in same session (same conversation_id reused automatically)
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Use the Machine Learning team","session_id":"demo-user-1"}'
```

Start an interactive chat session:
```bash
ouro-agents chat
```

Chat with an existing conversation thread:
```bash
ouro-agents chat --conversation-id <id>
```

Trigger a heartbeat tick:
```bash
ouro-agents heartbeat
```

## Subagents

The main agent can delegate focused work to built-in subagents such as
`research`, `planner`, `executor`, and `writer`.

You can set a default model for subagents and configure individual profiles in
`config.json`:

```json
"subagents": {
  "default_model": "google/gemini-2.5-flash",
  "writer": {
    "model": "anthropic/claude-sonnet-4"
  },
  "research": {
    "max_steps": 30
  }
}
```

- `subagents.default_model`: fallback model used by subagents when a profile
  does not specify its own override.
- `subagents.<name>.model`: choose a model for a specific subagent.
- `subagents.<name>.max_steps`: tune the agent loop for agent-mode
  subagents like `research` and `executor`.

You can also configure the human account that controls the agent:

```json
"controller": {
  "username": "your-handle"
}
```

- `controller.username`: the human Ouro username to mention as `{@username}` when a new plan
  enters review, so the quest is clearly flagged as ready for review.

## Run Modes

Main-agent config now lives under `modes.<name>` instead of being split
between `agent`, `planning`, and `heartbeat`.

```json
"modes": {
  "run": {
    "max_steps": 30
  },
  "chat": {
    "max_steps": 12
  },
  "planning": {
    "enabled": true,
    "model": "anthropic/claude-4.6-sonnet",
    "cadence": "4h",
    "min_heartbeats": 4,
    "review_window": "1h",
    "auto_approve": true,
    "max_steps": 6
  },
  "heartbeat": {
    "enabled": true,
    "every": "1h",
    "model": "openai/gpt-4.1-mini",
    "active_hours": {
      "start": "09:00",
      "end": "17:00",
      "timezone": "America/Chicago"
    },
    "max_steps": 8
  }
}
```

- `modes.run`: override the default steps for the main autonomous run mode.
- `modes.chat`: override the interactive chat loop.
- `modes.planning`: planning cadence/model settings plus the planning mode loop (`plan` internally).
- `modes.heartbeat`: heartbeat scheduler/model settings plus the heartbeat mode loop.
- `modes.chat-reply`: optionally override threaded reply runs separately from `chat`.
- Legacy top-level `planning` / `heartbeat`, `agent.max_steps`, `modes.overrides`, and `subagents.overrides` still load and are normalized into the flat shape.

## Prompt caching (OpenRouter + Anthropic)

`ouro-agents` supports Anthropic prompt caching through OpenRouter. Configure it in `config.json`:

```json
"prompt_caching": {
  "enabled": true,
  "ttl": "5m"
}
```

- `enabled`: turns Anthropic top-level `cache_control` on/off.
- `ttl`: `5m` (default) or `1h`.

This is only applied for models whose ID starts with `anthropic/`.
