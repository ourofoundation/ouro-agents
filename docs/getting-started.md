# Getting started

This walks you from PyPI to a standalone agent repository. You do not need an
`ouro-agents` source checkout.

## 1. Install

Create a virtual environment and install the released package:

```bash
python -m venv .venv
source .venv/bin/activate
pip install ouro-agents
```

Python 3.10+ is required.

## 2. Create an agent repository

```bash
ouro-agents init my-agent
cd my-agent
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
ouro-agents build-sandbox
```

The generated `pyproject.toml` pins the released runtime version. The repository
contains `agent.json`, `SOUL.md`, `HEARTBEAT.md`, `MEMORY.md`, `skills/`, and
`coils/`. It ignores credentials, opaque memory databases, conversations,
scratch work, and other runtime state.

## 3. Environment variables

The agent talks to OpenRouter and to Ouro. At minimum:

```bash
export OPENROUTER_API_KEY=sk-or-...
export OURO_API_KEY=ouro_...
# Optional: target a non-production backend
export OURO_BASE_URL=http://localhost:8003
```

Set these in the generated `.env` file. The CLI resolves `.env` relative to
`agent.json`, even when you invoke it from another directory. You can override
the path with `--env-file` or by setting `env_file` in `agent.json` (see
[Configuration reference](./configuration.md#env-file)).

`OPENROUTER_API_KEY` is the only required model-provider key — every
configurable model id is routed through OpenRouter.

If you use the search subagent, set `EXA_API_KEY` (the example config
plumbs it into the search MCP server).

`GH_TOKEN` is optional. Set it when the agent should use authenticated `git`
and `gh` commands in its Docker sandbox. Scope that credential to the agent's
own repository and protect the default branch — see
[GitHub identities](./github-identities.md).

## 4. Configure your agent

Edit `agent.json`. Minimum fields to set:

- `agent.name` — the display name of your agent (used everywhere).
- `agent.org_id` — the Ouro organization you want the agent to operate in.
- `models.strong` / `models.light` — OpenRouter model ids for agent roles.
- `security.controllers` — Ouro handles allowed to approve gated actions.

Everything else has reasonable defaults. The full schema is in
[Configuration reference](./configuration.md).

## 5. Repository and workspace

The generated repository is also the agent workspace (`agent.workspace` is
`.`). Durable identity, code, skills, coils, and curated memory are tracked:

```
my-agent/
├── SOUL.md
├── HEARTBEAT.md
├── MEMORY.md
├── skills/
├── coils/
├── protected/       # ignored runtime state
├── conversations/   # ignored runtime state
└── scratch/         # ignored temporary work
```

The generated config puts harness-owned state in `~/ouro-data/my-agent` and
links the ignored `protected/` path there. Conversation and scratch paths are
also ignored, so runtime activity does not pollute `git status`. See
[Workspace layout](./workspace.md).

## 6. First run

Run a one-off task:

```bash
ouro-agents --config agent.json run "What teams am I on?"
```

Or start an interactive chat:

```bash
ouro-agents --config agent.json chat
```

Or trigger a single heartbeat tick:

```bash
ouro-agents --config agent.json heartbeat
```

For the long-running mode (server + scheduled heartbeats + webhook receiver):

```bash
ouro-agents --config agent.json serve
```

This starts the FastAPI server on `server.host:server.port` (defaults
`0.0.0.0:8000`) with `/run`, `/health`, `/tasks`, and the webhook path from
`server.webhook_path` (default `/events`).

See the [CLI reference](./cli.md) for every flag and the
[HTTP API doc](./http-api.md) for endpoint details.

## 7. Where to go next

- Read [Concepts](./concepts.md) to understand modes, subagents, and memory.
- Tune behavior via the [Configuration reference](./configuration.md).
- Give the agent its own GitHub token and protect `main` ([GitHub identities](./github-identities.md)).
- Wire your agent into Ouro events (see [Events & webhooks](./events.md)).
