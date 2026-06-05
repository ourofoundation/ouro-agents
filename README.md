# ouro-agents

A Python package for running long-lived autonomous agents on the
[Ouro](https://ouro.foundation) platform. An agent process owns a
workspace on disk, talks to Ouro through MCP, maintains its own memory,
and runs in several modes — interactive chat, one-shot tasks, scheduled
heartbeats, and a multi-cycle planning loop tied to Ouro quests.

## Highlights

- **Multiple run modes** — chat, autonomous, heartbeat, plan, review,
  chat-reply. Each mode has a declarative profile controlling prompt
  framing, tool access, and lifecycle.
- **Subagents** — built-in `research`, `planner`, `executor`, `writer`,
  `developer` profiles, plus a parallel `delegate` tool for fan-out work.
  Custom profiles can be dropped into `workspace/subagents/`.
- **Three-layer memory** — vector memory (mem0 + Chroma) for curated
  facts, working memory (`MEMORY.md` and daily logs) maintained by the
  agent itself, and conversation state for chat continuity.
- **Doc store** — local markdown mirrored to Ouro posts, scoped per-team.
- **Planning loop** — generates plan cycles tied to Ouro quests, drives
  them across heartbeats, incorporates comment feedback through review
  heartbeats.
- **Scheduler** — heartbeat, consolidation, refinement, plus
  user-defined recurring tasks (cron or interval).
- **Refinement & cleanup** — periodic LLM-driven rewrites of working
  memory based on a typed change-set queue, plus deterministic cleanup
  for `asset.deleted` webhooks.
- **OpenRouter integration** — prompt caching for Anthropic models,
  per-mode and per-subagent reasoning effort, multi-model setups.

## Install

```bash
pip install -e .
```

Python 3.10+ is required.

## Quickstart

Set credentials and start an interactive chat:

```bash
export OPENROUTER_API_KEY=sk-or-...
export OURO_API_KEY=ouro_...

cp config.example.json config.json
# edit config.json: set agent.name, agent.model, agent.org_id, mcp_servers[].command

ouro-agents chat
```

Or run a one-shot task:

```bash
ouro-agents run "What teams am I on?"
```

Or start the long-running server (heartbeats + webhook receiver):

```bash
ouro-agents serve --config config.json
```

The full walkthrough is in [docs/getting-started.md](docs/getting-started.md).

## Documentation

Full docs live in [`docs/`](docs/README.md). A few starting points:

- [Concepts overview](docs/concepts.md) — how the agent loop, modes,
  subagents, memory, and planning fit together.
- [Configuration reference](docs/configuration.md) — every field in
  `config.json`.
- [CLI reference](docs/cli.md) — every subcommand and flag.
- [Run modes](docs/run-modes.md) — chat, autonomous, heartbeat, plan, review.
- [Subagents](docs/subagents.md) — built-in profiles, custom profiles,
  the `delegate` tool.
- [Memory model](docs/memory.md) — vector memory, doc store, working
  memory, reflection.
- [Workspace layout](docs/workspace.md) — what every directory is for.
- [Planning](docs/planning.md) — the plan / review cycle.
- [HTTP API & webhooks](docs/http-api.md) — `/run`, `/health`, event routing.
- [Glossary](docs/glossary.md) — recurring terms.

## CLI cheatsheet

```bash
ouro-agents serve --config config.json          # FastAPI server + scheduler
ouro-agents run "Summarize today's activity"    # one-shot autonomous run
ouro-agents chat [--conversation-id <id>]       # interactive REPL
ouro-agents heartbeat                           # one heartbeat tick
ouro-agents plan ["goal"] [--team-id <id>]      # force a planning heartbeat
ouro-agents review                              # force a review heartbeat
```

Add `-v` for verbose output or `--debug-md` to capture a full run trace
(see the [CLI reference](docs/cli.md)).

## HTTP API

While `ouro-agents serve` is running:

```bash
# Threaded conversation-style chat
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Hi, can you help me post a dataset?","session_id":"demo-user-1"}'

# Same session reuses the same conversation id automatically
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Use the Machine Learning team","session_id":"demo-user-1"}'

# Health
curl http://localhost:8000/health
```

The server also accepts Ouro webhook events at `server.webhook_path`
(default `/events`). See [docs/http-api.md](docs/http-api.md) and
[docs/events.md](docs/events.md).

## Workspace

The agent reads and writes everything under `agent.workspace` (default
`./workspace`):

```
workspace/
├── SOUL.md           # required: identity, values, operating rules
├── NOTES.md          # optional: ambient notes
├── MEMORY.md         # curated cross-team memory
├── conversations/    # per-conversation state and turns
├── shared/logs/      # period logs (daily/weekly/biweekly)
├── teams/<id>/       # team memory, logs/, plans, doc registry
├── memory/           # mem0 + Chroma store (opaque)
├── skills/           # workspace skill overrides
└── subagents/        # custom SubAgentProfile files
```

See [docs/workspace.md](docs/workspace.md).

## Configuration at a glance

Minimal `config.json` shape (full reference in
[docs/configuration.md](docs/configuration.md)):

```json
{
  "agent": {
    "name": "hermes",
    "model": "anthropic/claude-4.6-sonnet",
    "org_id": "00000000-0000-0000-0000-000000000000",
    "workspace": "./workspace"
  },
  "modes": {
    "run":      { "max_steps": 60 },
    "chat":     { "max_steps": 40 },
    "planning": { "enabled": true, "model": "anthropic/claude-4.6-sonnet", "cadence": "4h" },
    "heartbeat": {
      "enabled": true,
      "every": "1h",
      "model": "openai/gpt-4.1-mini",
      "active_hours": { "start": "09:00", "end": "17:00", "timezone": "America/Chicago" }
    }
  },
  "subagents": {
    "default_model": "google/gemini-2.5-flash",
    "writer":   { "model": "anthropic/claude-sonnet-4" },
    "research": { "max_steps": 30 }
  },
  "memory": {
    "provider": "mem0",
    "path": "./workspace/memory",
    "extraction_model": "google/gemini-2.5-flash",
    "embedder": "openai/text-embedding-3-small"
  },
  "mcp_servers": [
    {
      "name": "ouro",
      "transport": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "ouro_mcp.server"],
      "env": { "OURO_API_KEY": "${OURO_API_KEY}", "OURO_BASE_URL": "${OURO_BASE_URL}" }
    }
  ],
  "prompt_caching": { "enabled": true, "ttl": "5m" }
}
```

`controller.username` mentions a human `{@username}` when a plan enters
review. `subagents.<name>` overrides the model / max steps / reasoning
for any subagent profile. See the docs for everything else.

## Development

Tests are in `tests/`:

```bash
pytest
```

Linting:

```bash
ruff check .
```

The package layout:

```
ouro_agents/
├── agent.py            # OuroAgent orchestrator
├── runner.py           # CLI entry point
├── server.py           # FastAPI + webhook routing
├── config.py           # Pydantic config models + loader
├── modes/              # mode profiles + heartbeat + planning
├── subagents/          # SubAgentProfile + runner + built-in prompts
├── memory/             # vector memory + doc store + reflection
├── refinement/         # change-set queue + LLM-driven rewrites
├── cleanup/            # deterministic asset.deleted handler
├── skills/             # built-in markdown skills
├── tools/              # built-in tools (delegate, run_python, etc.)
├── tui/                # team / plan pickers
└── utils/              # streaming, callbacks, conversation helpers
```

Browse [docs/](docs/README.md) for a guided tour.

## License

See the repository root for license terms.
