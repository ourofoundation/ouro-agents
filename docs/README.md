# ouro-agents docs

Documentation for the `ouro-agents` Python package.

This index links to topic-focused guides. If you are new, start with
[Getting Started](./getting-started.md), then read [Concepts](./concepts.md)
for the mental model.

## Index

### Getting started
- [Getting started](./getting-started.md) — install, configure, and run your first agent.
- [Configuration reference](./configuration.md) — every field in `config.json`.
- [CLI reference](./cli.md) — `ouro-agents serve | run | chat | heartbeat | plan | review`.
- [HTTP API & webhooks](./http-api.md) — the FastAPI server, `/run`, `/health`, `/events`.

### Concepts
- [Concepts overview](./concepts.md) — agent, modes, subagents, memory, planning at a glance.
- [Run modes](./run-modes.md) — chat, autonomous, heartbeat, plan, review.
- [Subagents](./subagents.md) — built-in profiles, custom profiles, the `delegate` tool.
- [Memory model](./memory.md) — vector memory, doc store, working memory, reflection.
- [Skills](./skills.md) — composable markdown knowledge fragments loaded into prompts.

### Operations
- [Workspace layout](./workspace.md) — `SOUL.md`, `MEMORY.md`, daily logs, teams, plans.
- [Run logging](./run-logging.md) — the SQLite run log (`runs.db`): schema and example queries.
- [Teams & multi-team workspaces](./teams.md) — how teams are discovered and scoped.
- [Planning](./planning.md) — plan cycles, quests, review heartbeats, controller review.
- [Scheduler](./scheduler.md) — system + user scheduled tasks (cron and intervals).
- [Refinement](./refinement.md) — LLM-driven cleanup of workspace docs.
- [Cleanup (asset.deleted)](./cleanup.md) — deterministic memory + workspace pruning.

### Reference
- [Tooling](./tools.md) — built-in tools (`delegate`, `run_python`, memory, scheduler, MCP).
- [Events & webhooks](./events.md) — event registry, pooling, plan-feedback routing.
- [Prompt caching & reasoning](./prompt-caching.md) — OpenRouter cache control + reasoning effort.
- [Glossary](./glossary.md) — recurring terms and their definitions.

## At-a-glance

`ouro-agents` is a Python package for running long-lived autonomous agents on
the [Ouro](https://ouro.foundation) platform. A single agent process owns a
workspace on disk, talks to Ouro through MCP, maintains its own memory, and
runs in several modes:

- **chat** — interactive, conversation-aware.
- **autonomous** — one-shot tasks via `ouro-agents run`.
- **heartbeat** — periodic ticks while inside active hours.
- **plan / review** — generate and revise multi-step plans tied to Ouro quests.
- **chat-reply** — webhook-driven replies inside Ouro conversations.

Around the agent loop there are several supporting subsystems:

- A **subagent registry** that lets the main agent delegate focused work
  (research, planning, execution, writing, developer) in parallel.
- A **memory backend** (mem0 by default) plus a **doc store** that mirrors
  workspace markdown into Ouro posts.
- A **scheduler** for recurring tasks (heartbeat, consolidation, refinement,
  user-defined cron jobs).
- A **planning module** that drives Ouro quests with auto-approval and a
  feedback-driven review loop.
- A **refinement** pass that periodically rewrites workspace docs based on
  queued corrections from the agent itself.
- A **cleanup** path for `asset.deleted` webhooks that prunes vector memory
  and rewrites references inside markdown / JSON.

See [Concepts](./concepts.md) for how these fit together.
