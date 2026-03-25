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

You can set a default model for subagents and override individual profiles in
`config.json`:

```json
"subagents": {
  "default_model": "google/gemini-2.5-flash",
  "overrides": {
    "writer": {
      "model": "anthropic/claude-sonnet-4"
    },
    "research": {
      "max_steps": 30
    }
  }
}
```

- `subagents.default_model`: fallback model used by subagents when a profile
  does not specify its own override.
- `subagents.overrides.<name>.model`: choose a model for a specific subagent.
- `subagents.overrides.<name>.max_steps`: tune the agent loop for agent-mode
  subagents like `research` and `executor`.

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
