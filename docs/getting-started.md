# Getting started

This walks you from a fresh checkout to a running agent.

## 1. Install

`ouro-agents` is a Python package; install it editable from the repo:

```bash
pip install -e .
```

Python 3.10+ is required. Most installs benefit from a virtualenv (`.venv`
already exists in this repo if you prefer).

## 2. Environment variables

The agent talks to OpenRouter and to Ouro. At minimum:

```bash
export OPENROUTER_API_KEY=sk-or-...
export OURO_API_KEY=ouro_...
# Optional: target a non-production backend
export OURO_BASE_URL=http://localhost:8003
```

You can also set these in a `.env` file. The CLI will auto-load `.env` from
the working directory; you can override the path with `--env-file` or by
setting `env_file` in `config.json` (see
[Configuration reference](./configuration.md#env-file)).

`OPENROUTER_API_KEY` is the only required model-provider key — every
configurable model id is routed through OpenRouter.

If you use the search subagent, set `TAVILY_API_KEY` (the example config
plumbs it into the search MCP server).

## 3. Configure your agent

Copy the example and edit it:

```bash
cp config.example.json config.json
```

Minimum fields to set:

- `agent.name` — the display name of your agent (used everywhere).
- `agent.model` — default model id, e.g. `anthropic/claude-4.6-sonnet`.
- `agent.org_id` — the Ouro organization you want the agent to operate in.
- `mcp_servers[].command` — path to the Python interpreter that will run
  `ouro_mcp.server`. The example uses a pyenv path; change it to your own.
- `memory.extraction_model` and `memory.embedder` — the cheap model used by
  mem0 for fact extraction and the embedder for the vector store.

Everything else has reasonable defaults. The full schema is in
[Configuration reference](./configuration.md).

## 4. Make a workspace

The agent reads and writes to `agent.workspace` (default `./workspace`).
Create the directory and seed two files:

```
workspace/
├── SOUL.md      # identity, values, operating rules
└── NOTES.md     # optional ambient notes the agent reads each run
```

`SOUL.md` is the agent's persona — see the example shipped at
`workspace/SOUL.md` for a full template. Keep it short; it goes into every
system prompt.

The first time the agent runs it will populate the rest of the workspace
(see [Workspace layout](./workspace.md)).

## 5. First run

Run a one-off task:

```bash
ouro-agents run "What teams am I on?"
```

Or start an interactive chat:

```bash
ouro-agents chat
```

Or trigger a single heartbeat tick:

```bash
ouro-agents heartbeat
```

For the long-running mode (server + scheduled heartbeats + webhook receiver):

```bash
ouro-agents serve --config config.json
```

This starts the FastAPI server on `server.host:server.port` (defaults
`0.0.0.0:8000`) with `/run`, `/health`, `/tasks`, and the webhook path from
`server.webhook_path` (default `/events`).

See the [CLI reference](./cli.md) for every flag and the
[HTTP API doc](./http-api.md) for endpoint details.

## 6. Where to go next

- Read [Concepts](./concepts.md) to understand modes, subagents, and memory.
- Tune behavior via the [Configuration reference](./configuration.md).
- Wire your agent into Ouro events (see [Events & webhooks](./events.md)).
